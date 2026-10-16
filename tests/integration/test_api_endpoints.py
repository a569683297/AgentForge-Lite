"""
集成：HTTP 接口层（真打 FastAPI 应用；**只读**）
=================================================
标记 `db` —— 这些用例需要真实 Postgres / Redis（本地 compose 起的 db/redis）。

为什么敢打真库：这层要验的正是"路由 → service → ORM → 数据库"整条线通不通，
把 DB 换成 mock 就等于**把要验的东西自己删掉了**。
代价是这些用例必须**只读**：不建、不改、不删任何数据。
"""

import json

import pytest

pytestmark = pytest.mark.db


# ============================================================
# 健康检查
# ============================================================


async def test_health_reports_dependencies(api_client):
    """`/api/health` 是 Docker 健康检查探针用的那个端点（compose 里直接指向它）。"""
    resp = await api_client.get("/api/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "ok"
    assert body["deps"]["api"] == "ok"
    assert body["deps"]["redis"] == "ok"  # 真去 ping 了 Redis，不是写死的


# ============================================================
# 工具清单（D27）
# ============================================================


async def test_tools_endpoint_has_four_sections(api_client):
    """返回结构是前端"工具注册表"页的数据源，四个键各有分工，缺一不可。"""
    resp = await api_client.get("/api/tools")
    assert resp.status_code == 200

    body = resp.json()
    assert {"total", "by_source", "servers", "tools"} <= set(body)
    assert body["total"] == len(body["tools"])


async def test_by_source_partitions_the_flat_tool_list(api_client):
    """
    ★ 内部一致性断言（不依赖工具总数）：`by_source` 必须**不多不少**地
    把 `tools` 里的名字分完 —— 它是按 source 分组的，不是另一份独立清单。

    比"断言有 13 个工具"稳得多：后者会随接入的 server 变化而红，
    而这条永远成立（除非分组逻辑 itself 出错）。
    """
    body = (await api_client.get("/api/tools")).json()

    flat = sorted(t["name"] for t in body["tools"])
    grouped = sorted(name for names in body["by_source"].values() for name in names)
    assert flat == grouped

    # 每个 tools 条目的 source 必须与它落在的分组一致
    for tool in body["tools"]:
        assert tool["name"] in body["by_source"][tool["source"]]


async def test_tool_entries_are_json_serializable_and_carry_source(api_client):
    """`describe_tools()` 的每一条都必须能进 JSON（不能混进函数对象），且带 source。"""
    body = (await api_client.get("/api/tools")).json()
    for tool in body["tools"]:
        assert isinstance(tool["name"], str)
        assert isinstance(tool["source"], str)
        assert isinstance(tool["parameters"], dict)


async def test_servers_block_answers_a_different_question(api_client):
    """
    `tools` 说"现在有哪些工具"，`servers` 说"这些工具的主人现在什么情况"。
    两者可以不一致 —— 那种不一致本身就是要看见的信息。
    """
    body = (await api_client.get("/api/tools")).json()
    assert isinstance(body["servers"], list)


# ============================================================
# 评测接口（D21 / D22）
# ============================================================


async def test_eval_cases_list_returns_list(api_client):
    resp = await api_client.get("/api/eval/cases")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_eval_cases_reject_invalid_category_with_422(api_client):
    """
    ★ 非法类别必须 422（由 `Literal` 类型自动拦），**不能**静默查空返回 `[]`。

    后者会伪装成"这个类别暂时没有题"，而真相是参数打错了 ——
    这类"看起来正常的错答案"最难查。
    """
    resp = await api_client.get("/api/eval/cases", params={"category": "不存在的类别"})
    assert resp.status_code == 422


async def test_missing_case_returns_404_not_500(api_client):
    """不存在的 case_key 是**用户输入问题**（404），不是服务端故障（500）。"""
    resp = await api_client.get("/api/eval/cases/NOPE-999")
    assert resp.status_code == 404


async def test_dataset_fingerprint_is_stable_across_calls(api_client):
    """
    ★ "冻结"这条纪律的**可验证出口**：D23 报告里写一个指纹，
    它必须与这里读到的一致；不一致就说明题目被改过、报告作废。
    连读两次必须完全相同（否则连"一次运行内自洽"都做不到）。
    """
    first = (await api_client.get("/api/eval/dataset")).json()
    second = (await api_client.get("/api/eval/dataset")).json()
    assert first == second


# ============================================================
# 会话接口（D14）
# ============================================================


async def test_sessions_list_returns_list(api_client):
    resp = await api_client.get("/api/sessions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_unknown_route_is_404(api_client):
    """未知路径要干净地 404（被全局异常处理器兜成 500 就说明路由层有问题）。"""
    resp = await api_client.get("/api/这个路径不存在")
    assert resp.status_code == 404


# ============================================================
# 对话接口（D11；D36 新增防回归）
# ============================================================


async def test_chat_forwards_every_chatresult_field_to_http(api_client, monkeypatch):
    """
    ★ 防回归（D36）：`/api/chat` 必须把 `ChatResult` 的**每个字段**原样搬进响应。

    D36 踩过的坑：路由构造 `ChatResponse(...)` 时**漏传 `tool_calls`**。该字段有
    `default_factory=list`，漏传**不报错、不崩溃** —— 响应里 `tool_calls` 恒为 `[]`，
    前端「工具卡片」永远不显示（刷新后从 PG 重读才出现，伪装成前端 bug）。
    这类"给模型加字段、忘了同步搬"是**静默**的，只能靠跨层比对抓。

    ⚠ 为什么不能只断言"响应键集合 == 模型字段集合"：
      pydantic / FastAPI 序列化**会带上默认值** —— 漏传的字段在 JSON 里照样出现
      （只是值退回了默认）。所以键集合比对对这个 bug **恒真**，抓不到。
    正确做法：把 `run_agent` 换成返回**每个字段都带哨兵值**的桩，
    再断言哨兵能原样读回 —— 漏传哪个字段，哪个就退回默认值 → 这边红。
    """
    from app.api import chat as chat_module
    from app.schemas.chat import (
        ChatResponse,
        ChatResult,
        MetricResultItem,
        ModelUsed,
        SourceItem,
    )

    fake = ChatResult(
        answer="哨兵回答",
        sources=[
            SourceItem(
                index=7,
                source="SENTINEL_DOC",
                content="哨兵片段",
                similarity=0.4242,
                page_ref="p.42",
            )
        ],
        invalid_citations=[13],
        tool_calls=["SENTINEL_TOOL"],
        # S2：元断言逼出来的新哨兵（就是下面那条 assert 设计的用途）
        model_used=ModelUsed(id="sentinel-id", name="哨兵模型", model="sentinel-model-v9"),
        # D37 块 3：指标结果的转发哨兵（同样被元断言逼出来的）
        metric_results=[
            MetricResultItem(
                metric="SENTINEL_METRIC",
                label="哨兵指标",
                dimension="sentinel_dim",
                source="sql",
                start_date="2026-01-01",
                end_date="2026-01-02",
                rows=[{"dim": "x", "value": 42}],
            )
        ],
    )

    # 签名对齐路由里的调用（S2 新增关键字参数 model_id）
    async def _fake_run_agent(session_id, message, *, model_id=None):
        return fake

    monkeypatch.setattr(chat_module, "run_agent", _fake_run_agent)

    resp = await api_client.post("/api/chat", json={"message": "任意一句话"})
    assert resp.status_code == 200
    body = resp.json()

    # 逐字段原样读回：漏传哪个，哪个就退回默认值 —— 下行就是 D36 的真 bug 所在
    assert body["answer"] == "哨兵回答"
    assert body["invalid_citations"] == [13]
    assert body["tool_calls"] == ["SENTINEL_TOOL"]
    assert [s["source"] for s in body["sources"]] == ["SENTINEL_DOC"]
    assert body["sources"][0]["page_ref"] == "p.42"
    # S2：model_used 漏传的话这里会退回 None —— 归因静默丢失（与 D36 同类的坑）
    assert body["model_used"] == {
        "id": "sentinel-id",
        "name": "哨兵模型",
        "model": "sentinel-model-v9",
    }
    # D37 块 3：metric_results 漏传的话这里会退回 [] —— 前端图表数据静默消失
    assert body["metric_results"] == [
        {
            "metric": "SENTINEL_METRIC",
            "label": "哨兵指标",
            "dimension": "sentinel_dim",
            "source": "sql",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "degraded": False,
            "error": None,
            "rows": [{"dim": "x", "value": 42}],
        }
    ]

    # 轻量一致性：不出现模型里没有的键、也不缺键。
    # （对"默认值兜底"型 bug 恒真，真正的守卫是上面的哨兵断言。）
    assert set(body) == set(ChatResponse.model_fields)

    # ★ 元断言：桩必须覆盖 ChatResult 的**全部**字段。
    # 将来给 ChatResult 加字段时这条会红，逼你回来补哨兵 —— 不让新字段裸奔。
    # （S2 加 model_used 时它确实红了，哨兵就是按它要求补的；
    #   D37 加 metric_results 时同样红了一次 —— 机制在正常工作。）
    covered = {
        "answer",
        "sources",
        "invalid_citations",
        "tool_calls",
        "model_used",
        "metric_results",
    }
    assert covered == set(ChatResult.model_fields), (
        "ChatResult 字段变了：请在桩里为新字段补哨兵值，并加一条能原样读回的断言"
    )


# ============================================================
# 流式对话接口（M6 优化②）
# ============================================================


async def test_chat_stream_forwards_service_events_as_sse_frames(api_client, monkeypatch):
    """
    ★ 流式出口的结构守卫：`run_agent_stream` 产出的每个事件，必须**原样**变成一条 SSE 帧。

    D36 的教训在流式出口同样成立：done 帧里 `tool_calls` / `invalid_citations`
    这类"有默认值、漏传不报错"的字段，用哨兵值钉死 —— 漏传哪个，哪个退回默认值 → 这边红
    （与 test_chat_forwards_every_chatresult_field_to_http 对称，一条守非流式、一条守流式）。

    顺带钉住帧协议三个硬约束（前端解析器就按它实现）：
      ① 第一帧必须是 meta 且带 session_id（前端靠它确认"流已建立"）；
      ② 帧分隔符 `\\n\\n`、每帧一个 `data: ` 行、JSON 可解析；
      ③ done 是终帧。
    """
    from app.api import chat as chat_module
    from app.schemas.chat import SourceItem

    # 签名对齐路由里的调用（S2 新增关键字参数 model_id）
    async def _fake_stream(session_id, message, *, model_id=None):
        yield {"type": "token", "text": "哨兵"}
        yield {"type": "reset"}
        yield {"type": "tool", "name": "SENTINEL_TOOL", "phase": "start"}
        yield {"type": "tool", "name": "SENTINEL_TOOL", "phase": "end", "ok": True}
        yield {
            "type": "done",
            "session_id": str(session_id),
            "answer": "哨兵回答",
            "sources": [
                SourceItem(
                    index=7,
                    source="SENTINEL_DOC",
                    content="哨兵片段",
                    similarity=0.4242,
                    page_ref="p.42",
                ).model_dump()
            ],
            "invalid_citations": [13],
            "tool_calls": ["SENTINEL_TOOL"],
            # S2：done 帧的归因哨兵
            "model_used": {"id": "sentinel-id", "name": "哨兵模型", "model": "sentinel-model-v9"},
            # D37 块 3：done 帧的指标结果哨兵（漏传 → 前端收不到图表数据）
            "metric_results": [
                {
                    "metric": "SENTINEL_METRIC",
                    "label": "哨兵指标",
                    "dimension": None,
                    "source": "sql",
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-02",
                    "degraded": False,
                    "error": None,
                    "rows": [{"value": 42}],
                }
            ],
        }

    monkeypatch.setattr(chat_module, "run_agent_stream", _fake_stream)

    resp = await api_client.post("/api/chat/stream", json={"message": "任意一句话"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # SSE 三件套里的两件：不缓存 + 告诉 nginx "这条响应不要缓冲"
    # （漏了后者，8080 上的症状是"后端在逐字发、界面一次性蹦出"）
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"

    frames = [frame for frame in resp.text.split("\n\n") if frame]
    assert frames, "响应体里没有任何 SSE 帧"
    events = []
    for frame in frames:
        assert frame.startswith("data: "), f"帧不是 data: 行开头：{frame!r}"
        events.append(json.loads(frame[len("data: "):]))

    assert events[0]["type"] == "meta"
    assert events[0]["session_id"]  # 服务端补全的会话 id
    assert [e["type"] for e in events[1:]] == ["token", "reset", "tool", "tool", "done"]
    assert events[1]["text"] == "哨兵"

    done = events[-1]
    # 哨兵逐字段读回 —— 漏传 / 被默认值吞掉，这里就红
    assert done["answer"] == "哨兵回答"
    assert done["tool_calls"] == ["SENTINEL_TOOL"]
    assert done["invalid_citations"] == [13]
    assert [s["source"] for s in done["sources"]] == ["SENTINEL_DOC"]
    assert done["sources"][0]["page_ref"] == "p.42"
    # S2：归因字段也不能漏（漏了 → 帧里没有 / 为 null → 前端归因静默消失）
    assert done["model_used"] == {
        "id": "sentinel-id",
        "name": "哨兵模型",
        "model": "sentinel-model-v9",
    }
    # D37 块 3：指标结果字段同理（漏了 → 帧里退回 [] → 图表数据静默消失）
    assert done["metric_results"] == [
        {
            "metric": "SENTINEL_METRIC",
            "label": "哨兵指标",
            "dimension": None,
            "source": "sql",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "degraded": False,
            "error": None,
            "rows": [{"value": 42}],
        }
    ]


async def test_chat_stream_rejects_empty_message_with_422(api_client):
    """参数校验与 /api/chat 同一收口（pydantic min_length=1）—— "流式"不是绕过校验的理由。"""
    resp = await api_client.post("/api/chat/stream", json={"message": ""})
    assert resp.status_code == 422


# ============================================================
# S2 模型切换：接口层的两条翻译 + 透传守卫
# ============================================================


async def test_chat_maps_model_resolution_error_to_422(api_client, monkeypatch):
    """
    S2：用户选了个不存在 / 被禁用的模型 → **422**（不是 500）。

    异常类型 → HTTP 语义的翻译是接口层的职责（服务层只抛 `ModelResolutionError`、
    不 import FastAPI）；422 = "你的输入有问题"，前端能像处理 message 为空
    （同样是 422 + detail）一样统一处理。
    """
    from app.api import chat as chat_module
    from app.services.llm_gateway import ModelResolutionError

    async def _fake_run_agent(session_id, message, *, model_id=None):
        raise ModelResolutionError("所选模型不存在（可能已被删除）：deadbeef")

    monkeypatch.setattr(chat_module, "run_agent", _fake_run_agent)

    resp = await api_client.post(
        "/api/chat", json={"message": "你好", "model_id": "deadbeef"}
    )
    assert resp.status_code == 422
    assert "不存在" in resp.json()["detail"]


async def test_chat_stream_maps_model_resolution_error_to_error_frame(api_client, monkeypatch):
    """
    S2：同一个错误的流式翻译 → **error 帧**（HTTP 200 在开流时已先行，状态码改不了）。

    断言帧序列 = [meta, error]（meta 是接口层先发的，异常发生在服务层事件流开始后），
    且 message 是原始可读原因 —— 前端拿到能直接展示，不需要自己猜。
    """
    from app.api import chat as chat_module
    from app.services.llm_gateway import ModelResolutionError

    async def _fake_stream(session_id, message, *, model_id=None):
        raise ModelResolutionError("所选模型已被禁用：测试模型")
        yield  # pragma: no cover —— 语法上保住生成器身份；此行永不可达

    monkeypatch.setattr(chat_module, "run_agent_stream", _fake_stream)

    resp = await api_client.post(
        "/api/chat/stream", json={"message": "你好", "model_id": "deadbeef"}
    )
    # 关键：HTTP 层是 200（头先于内容发出），错误只能靠帧传递
    assert resp.status_code == 200
    frames = [frame for frame in resp.text.split("\n\n") if frame]
    events = [json.loads(frame[len("data: "):]) for frame in frames]
    assert [e["type"] for e in events] == ["meta", "error"]
    assert "禁用" in events[-1]["message"]


async def test_chat_passes_model_id_through_to_service(api_client, monkeypatch):
    """
    传输层守卫：请求体里的 `model_id` 原样到服务层；
    **不传时是 None**（= 现有默认链路，评测 / 旧脚本零影响的数据证据）。
    """
    from app.api import chat as chat_module
    from app.schemas.chat import ChatResult

    seen: dict = {}

    async def _fake_run_agent(session_id, message, *, model_id=None):
        seen["model_id"] = model_id
        return ChatResult(answer="ok")

    monkeypatch.setattr(chat_module, "run_agent", _fake_run_agent)

    resp = await api_client.post("/api/chat", json={"message": "你好", "model_id": "deepseek"})
    assert resp.status_code == 200
    assert seen["model_id"] == "deepseek"

    resp = await api_client.post("/api/chat", json={"message": "你好"})
    assert resp.status_code == 200
    assert seen["model_id"] is None
