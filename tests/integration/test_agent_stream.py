"""
集成：Agent 流式路径（M6 优化②）—— run_agent_stream 全链路
=============================================================
与 test_agent_paths.py 完全对称：真跑 LangGraph 图（这次走 astream 双模式，
custom 事件 + values 快照），**只把最内层的 LLM 调用换成 mock**。

钉住一条核心不变量：

    "流式与非流式是同一条业务链路的两个出口 —— 产物逐字段一致，
      差异只在'token 什么时候被看到'。"

以及三个流式特有行为：
    - 直答路径：事件流 = token 分片 + done（无工具事件）；
    - 工具路径：tool start 在**执行前**、tool end 在**执行后**（ok 与 span 判定同源），
      最终答案的 token 在工具结束之后才出现 —— "边查边答"的顺序可见；
    - 边界（reset）：模型先说了"预告"再改去调工具 —— 预告 token 已发给用户，
      必须补一帧 reset 撤回，且最终 answer 里不含预告文本。

标记 `db`：run_agent_stream 会读 Redis 热窗口与 PG 摘要。
（统一 persist=False —— 只读，不写任何会话数据。）
"""

import uuid

import pytest

pytestmark = pytest.mark.db


class MockStreamLLM:
    """
    streaming 版 MockLLM：用一个对象同时顶掉 `_call_once` 与 `_call_once_stream` 两个调用点。

    为什么一个对象够：两个调用点的**返回结构本来就同构**
    （{"data": {"choices": [{"message": ...}]}, "usage": ...}），差别只在流式版会
    额外通过 `on_content` 分片回调。按"是否收到 on_content"自动区分行为：
      - run_agent（非流式）走 `_call_once`：不回调，只返回整段 message；
      - run_agent_stream 走 `_call_once_stream`：先分片回调，再返回同一段 message。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.reply = "（mock）好的"
        self.tool_calls: list | None = None
        # 首轮"预告文本"（专测 reset 事件）：调工具前先说的话
        self.first_content: str | None = None

    async def __call__(
        self,
        provider: dict,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        trace_name: str = "",
        gen=None,
        temperature: float = 0.4,
        on_content=None,
    ) -> dict:
        self.calls.append(
            {"model": provider.get("model"), "messages": messages, "tools": tools}
        )

        if tools and self.tool_calls and len(self.calls) == 1:
            # 第一轮：声明工具调用（可带一段预告文本）
            if self.first_content and on_content is not None:
                on_content(self.first_content)
            message = {
                "role": "assistant",
                "content": self.first_content,
                "tool_calls": self.tool_calls,
            }
        else:
            message = {"role": "assistant", "content": self.reply}
            if on_content is not None:
                # 分两片吐 —— "逐字输出"在事件层的等价物
                half = max(1, len(self.reply) // 2)
                on_content(self.reply[:half])
                on_content(self.reply[half:])

        return {
            "data": {"choices": [{"message": message}]},
            "usage": {"total_tokens": 7},
        }


@pytest.fixture
def mock_stream_llm(monkeypatch):
    """
    把 `_call_once` / `_call_once_stream` 一起换成 MockStreamLLM，并静音 Langfuse。

    ⚠ 为什么两个都换：本模块要**对照运行**两条出口（run_agent vs run_agent_stream），
      它们分别命中这两个调用点 —— 用同一个对象替换，"两边的 LLM 输出完全一样"
      才成为事实，对照断言才是公平的。
    """
    from tests.conftest import FakeLangfuse
    from app.services import llm_gateway as gateway

    fake = MockStreamLLM()
    monkeypatch.setattr(gateway, "_call_once", fake)
    monkeypatch.setattr(gateway, "_call_once_stream", fake)
    monkeypatch.setattr(gateway, "langfuse", FakeLangfuse())
    return fake


async def test_stream_direct_answer_matches_non_streaming(mock_stream_llm, llm_channels_ready):
    """
    路径 ①（直答）流式：事件流 = token 分片 + done，无工具/无 reset。

    ★ 核心断言：token 拼接 == done.answer == 非流式同输入的 answer
      ——"流式不影响产物"这条不变量在这里被钉住。
    """
    from app.services.agent_service import run_agent, run_agent_stream

    mock_stream_llm.reply = "这是直答"

    baseline = await run_agent(uuid.uuid4(), "你好", persist=False)
    events = [event async for event in run_agent_stream(uuid.uuid4(), "你好", persist=False)]

    types = [event["type"] for event in events]
    assert types[-1] == "done", "done 必须是终帧"
    # meta 是**接口层**补的（run_agent_stream 不管它）；这里不该出现
    assert types.count("done") == 1
    assert "token" in types
    assert "tool" not in types and "reset" not in types

    streamed_answer = "".join(event["text"] for event in events if event["type"] == "token")
    done = events[-1]
    assert streamed_answer == "这是直答", "token 拼接必须等于回答"
    assert done["answer"] == baseline.answer, "流式与非流式的 answer 必须逐字一致"
    assert done["tool_calls"] == baseline.tool_calls == []
    assert done["sources"] == [] and done["invalid_citations"] == []


async def test_stream_tool_path_emits_start_end_around_execution(mock_stream_llm, llm_channels_ready):
    """
    路径 ②（工具）流式：执行**前**发 tool start、执行**后**发 tool end（ok=true），
    最终答案的 token 在工具结束**之后**才出现 —— "Agent 边查边答"的时间顺序靠它。
    """
    from app.services.agent_service import run_agent_stream

    mock_stream_llm.tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "current_time", "arguments": "{}"},
        }
    ]
    mock_stream_llm.reply = "现在是 10 点"

    events = [event async for event in run_agent_stream(uuid.uuid4(), "现在几点", persist=False)]

    tool_events = [event for event in events if event["type"] == "tool"]
    assert [event["phase"] for event in tool_events] == ["start", "end"], (
        "工具事件必须成对且顺序正确（start 前于 end）"
    )
    assert all(event["name"] == "current_time" for event in tool_events)
    assert tool_events[1]["ok"] is True, "current_time 是已注册的本地工具，不该走失败分支"

    first_answer_token = next(
        index for index, event in enumerate(events) if event["type"] == "token"
    )
    tool_end_index = events.index(tool_events[1])
    assert tool_end_index < first_answer_token, "最终答案的 token 必须在工具结束之后"

    done = events[-1]
    assert done["tool_calls"] == ["current_time"]


async def test_stream_reset_retracts_intermediate_preface(mock_stream_llm, llm_channels_ready):
    """
    边界（reset 撤回）：模型先说了一句"预告"再改去调工具。

    预告文本的 token 已经发给用户了（发出去的收不回来），必须补一帧 reset 让前端清掉；
    最终 answer 里**不含**预告文本 —— 保证"屏幕上留下的文字 == done 里的 answer"。
    """
    from app.services.agent_service import run_agent_stream

    mock_stream_llm.tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "current_time", "arguments": "{}"},
        }
    ]
    mock_stream_llm.first_content = "我来查一下。"
    mock_stream_llm.reply = "现在是 10 点"

    events = [event async for event in run_agent_stream(uuid.uuid4(), "现在几点", persist=False)]

    types = [event["type"] for event in events]
    assert "reset" in types, "预告文本之后必须有 reset"
    assert types.index("reset") == types.index("token") + 1, (
        "reset 必须紧跟预告 token（预告是第一个 token，reset 是它的直接下场）"
    )
    assert types.index("reset") < types.index("tool"), "reset 要先于工具事件"

    done = events[-1]
    assert done["answer"] == "现在是 10 点"
    assert "我来查一下" not in done["answer"], "预告文本不得混进最终回答"
