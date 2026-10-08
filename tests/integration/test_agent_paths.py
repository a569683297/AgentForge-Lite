"""
集成：Agent 三路径（直答 / 工具 / RAG）—— PRD §14 点名
========================================================
真跑 LangGraph 图（`run_agent` 全链路），**只把最内层的 LLM 调用换成 mock** ——
于是"图怎么走、消息怎么拼、最终答案从哪条消息里取"全是真实代码在跑。

标记 `db`：`run_agent` 会读 Redis 热窗口与 PG 摘要。
（这里统一用 `persist=False`，所以它**不写**任何会话数据 —— 见 run_agent 的文档。）
"""

import uuid

import pytest

pytestmark = pytest.mark.db


async def test_direct_answer_path(mock_llm, llm_channels_ready):
    """
    路径 ①：LLM 不调工具，直接给答案 → 图在 plan 之后立刻 END。

    ★ 顺带断言"只调了一次 LLM"：这正是 D14 修复的目标
      （修复前 plan 说完话还要走一个 answer 节点再调一次，一轮白烧一次调用）。
    """
    from app.services.agent_service import run_agent

    mock_llm.reply = "这是直答"

    result = await run_agent(uuid.uuid4(), "你好", persist=False)

    assert result.answer == "这是直答"
    assert result.tool_calls == []
    assert len(mock_llm.calls) == 1, "无工具轮只该调一次 LLM"


async def test_tool_path_executes_tool_then_answers(mock_llm, llm_channels_ready):
    """
    路径 ②：LLM 声明调工具 → 图走 execute → 结果写回 → 再规划 → 直答。

    断言分两层：
      a) `result.tool_calls` 记下了这次调用（评测判定 tool_miss 靠它）
      b) 第二轮 LLM **真的看到了工具结果**（messages 里有 role=tool 且不是"未知工具"）
         —— 只看 a) 是不够的：工具完全可能被调了但执行失败。
    """
    from app.services.agent_service import run_agent

    mock_llm.tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "current_time", "arguments": "{}"},
        }
    ]
    mock_llm.reply = "现在是 10 点"

    result = await run_agent(uuid.uuid4(), "现在几点", persist=False)

    assert result.tool_calls == ["current_time"]
    assert result.answer == "现在是 10 点"
    assert len(mock_llm.calls) == 2, "有工具轮 = 决策一次 + 作答一次"

    tool_messages = [m for m in mock_llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1, "工具结果没有以 tool 消息写回"
    assert "未知工具" not in tool_messages[0]["content"], "工具没注册，走的是失败分支"
    assert "执行失败" not in tool_messages[0]["content"]


async def test_rag_path_calls_retrieval_tool(mock_llm, llm_channels_ready):
    """
    路径 ③：LLM 决定查知识库 → 真打 pgvector + BM25 混合检索。

    ⚠ 只断言"检索工具被调了、且没走失败分支"，**不断言召回内容** ——
      后者取决于库里有没有数据、切片质量如何，属于 D20–D24 评测层的职责。
      在这里断言召回，会让这条用例随语料变化而红，那不是回归。

    ⚠ 工具名不写死：从注册表里按语义找（零硬编码纪律）。
    """
    from app.services.agent_service import run_agent
    from app.tools.registry import list_tools

    candidates = [n for n in list_tools() if "search" in n or "retriev" in n]
    assert candidates, f"注册表里没有检索类工具：{list_tools()}"
    tool_name = sorted(candidates)[0]

    mock_llm.tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": tool_name, "arguments": '{"query": "年假有几天"}'},
        }
    ]
    mock_llm.reply = "根据公司制度……"

    result = await run_agent(uuid.uuid4(), "年假有几天", persist=False)

    assert result.tool_calls == [tool_name]

    tool_messages = [m for m in mock_llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_messages, "工具结果没写回"
    assert "未知工具" not in tool_messages[0]["content"]


async def test_hallucinated_citation_is_reported_not_silently_kept(mock_llm, llm_channels_ready):
    """
    引用校验（D11）：回答里出现来源表里不存在的编号要**被记下来**。

    直答路径没有任何来源，所以回答里写 [1] 必然越界 ——
    这正好是"LLM 幻觉引用"的最小复现。
    设计上只检测、不改写回答（改写会引入新的不确定性），所以断言的是
    `invalid_citations` 有值，而 `answer` 原样保留。
    """
    from app.services.agent_service import run_agent

    mock_llm.reply = "根据资料 [1]，年假是 5 天。"

    result = await run_agent(uuid.uuid4(), "年假有几天", persist=False)

    assert result.sources == []
    assert result.invalid_citations == [1]
    assert result.answer == "根据资料 [1]，年假是 5 天。"
