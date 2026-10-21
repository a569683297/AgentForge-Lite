"""
Trace 服务的纯函数与降级路径
============================
这些用例不碰网络：`LangfuseClient` 被替换成一个按参数返回固定明细的假对象。
真正要守住的是三件事 ——

  1. **树的形状**：父子关系、depth、相对时间偏移（画瀑布图全靠这三个）；
  2. **不完整的树也要画**：父节点被截断时，孤儿必须变成根，而不是被丢掉；
  3. **降级不是错误**：Langfuse 读不到时返回 degraded=true，不是抛异常让整页 500。
"""

from __future__ import annotations

import pytest

from app.services import trace_service
from app.services.langfuse_client import LangfuseUnavailableError


def _obs(
    span_id: str,
    name: str,
    *,
    parent: str | None = None,
    start: str,
    end: str,
    type_: str = "SPAN",
    trace: str = "t-1",
    session: str | None = "s-1",
    level: str | None = None,
    status_message: str | None = None,
) -> dict:
    """造一条 v2 observations 的明细（只保留被测代码真正读的字段）。"""
    return {
        "id": span_id,
        "name": name,
        "parentObservationId": parent,
        "startTime": start,
        "endTime": end,
        "type": type_,
        "traceId": trace,
        "sessionId": session,
        "level": level,
        "statusMessage": status_message,
        "latency": None,          # 让它走「首尾时间差」分支
        "modelId": None,
    }


class _FakeClient:
    """按需返回固定明细或固定异常的替身。"""

    def __init__(self, rows: list[dict] | None = None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls: list[dict] = []

    async def fetch_v2_observations(self, **kwargs) -> list[dict]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.rows


# ---------------------------------------------------------------- 建树


def test_build_spans_tree_shape() -> None:
    """三层树：depth 递增、相对时间以最早 span 为 0 点、父一定排在子前面。"""
    spans, total_ms = trace_service._build_spans(
        [
            _obs("root", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:01.000Z", type_="AGENT"),
            _obs("plan", "agent-plan", parent="root", start="2026-10-20T07:00:00.100Z", end="2026-10-20T07:00:00.600Z", type_="GENERATION"),
            _obs("tool", "tool:search_documents", parent="root", start="2026-10-20T07:00:00.600Z", end="2026-10-20T07:00:00.900Z", type_="TOOL"),
            _obs("emb", "embedding", parent="tool", start="2026-10-20T07:00:00.650Z", end="2026-10-20T07:00:00.700Z"),
        ]
    )

    assert [s["name"] for s in spans] == [
        "run_agent",
        "agent-plan",
        "tool:search_documents",
        "embedding",
    ], "必须先序：父在子之前"
    assert [s["depth"] for s in spans] == [0, 1, 1, 2]
    assert spans[0]["start_ms"] == 0.0, "最早的 span 必须被归零（瀑布图横轴原点）"
    assert spans[3]["start_ms"] == 650.0, "相对偏移按毫秒算"
    assert round(total_ms) == 1000


def test_build_spans_orphan_becomes_root() -> None:
    """
    父节点不在结果集里（被时间窗或 limit 截掉）→ 孤儿当根，**不能丢**。

    丢掉的表现是「明明有 6 个 span，页面只画 4 个」且不报错 —— 最难发现的一类。
    """
    spans, _ = trace_service._build_spans(
        [
            _obs("a", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:00.500Z"),
            _obs("b", "tool:x", parent="gone", start="2026-10-20T07:00:00.100Z", end="2026-10-20T07:00:00.200Z"),
        ]
    )
    assert len(spans) == 2
    assert {s["name"]: s["depth"] for s in spans} == {"run_agent": 0, "tool:x": 0}


def test_build_spans_marks_error() -> None:
    """level=ERROR → status=error（页面据此把条染红）；正常的条是 ok。"""
    spans, _ = trace_service._build_spans(
        [
            _obs("a", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:01.000Z", level="ERROR"),
            _obs("b", "tool:x", parent="a", start="2026-10-20T07:00:00.100Z", end="2026-10-20T07:00:00.200Z"),
        ]
    )
    assert [s["status"] for s in spans] == ["error", "ok"]


def test_build_spans_warning_is_not_error() -> None:
    """
    WARNING / statusMessage → `warning`，**不是** `error`。

    这是接上真实数据后暴露的误导：重排超时降级写的是
    `level="WARNING"` + `statusMessage="重排超时降级（返回融合序）"`，
    而旧逻辑"有 statusMessage 就算 error"把它染成和 500 一样的红色。
    实测那次对话 rerank 3006ms 超时，整条 trace 看起来"重排挂了"，
    实际只是退回了融合序。降级要调超时，错误要查堆栈 —— 不能混。
    """
    spans, _ = trace_service._build_spans(
        [
            _obs("a", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:01.000Z"),
            _obs(
                "b",
                "rerank",
                parent="a",
                start="2026-10-20T07:00:00.100Z",
                end="2026-10-20T07:00:03.100Z",
                level="WARNING",
                status_message="重排超时降级（返回融合序）",
            ),
        ]
    )
    assert [s["status"] for s in spans] == ["ok", "warning"]
    # 原因要透传出去：页面的 tooltip 靠它说明"为什么是黄的"
    assert spans[1]["status_message"] == "重排超时降级（返回融合序）"


def test_has_error_ignores_warning() -> None:
    """
    警告不算错误 —— 否则列表上的红点会被降级刷屏，真错误反而被淹没。

    实测 24 小时窗口里，重排降级是常态（容器内 2.4~3.0s 贴着 3s 超时线），
    若把它算进 has_error，左列表会几乎每条都带红点。
    """
    assert trace_service._has_error([{"level": "ERROR"}]) is True
    assert trace_service._has_error([{"level": "WARNING", "statusMessage": "降级"}]) is False
    assert trace_service._has_error([{"level": "DEFAULT"}]) is False


# ---------------------------------------------------------------- 列表


async def test_list_traces_groups_by_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 trace 的 span 收成一行；不同 trace 分开；按开始时间倒序。"""
    fake = _FakeClient(
        rows=[
            _obs("a1", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:02.000Z", trace="t-old"),
            _obs("a2", "agent-plan", parent="a1", start="2026-10-20T07:00:00.100Z", end="2026-10-20T07:00:00.600Z", trace="t-old"),
            _obs("b1", "run_agent", start="2026-10-20T08:00:00.000Z", end="2026-10-20T08:00:01.000Z", trace="t-new"),
        ]
    )
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: fake)

    result = await trace_service.list_traces(hours=24, limit=10)

    assert result["degraded"] is False
    assert [t["trace_id"] for t in result["traces"]] == ["t-new", "t-old"], "必须新的在前"
    assert result["traces"][1]["span_count"] == 2
    assert round(result["traces"][1]["duration_ms"]) == 2000
    assert result["traces"][0]["name"] == "run_agent", "名字取根 span"


async def test_list_traces_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _obs(f"r{i}", "run_agent", start=f"2026-10-20T0{i}:00:00.000Z", end=f"2026-10-20T0{i}:00:01.000Z", trace=f"t{i}")
        for i in range(1, 5)
    ]
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: _FakeClient(rows=rows))

    result = await trace_service.list_traces(hours=24, limit=2)
    assert len(result["traces"]) == 2


# ---------------------------------------------------------------- 降级 / 缺失


async def test_list_traces_degraded_is_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    读通道不可用 → degraded=true + reason，**不抛异常**。

    这是产品语义：观测数据是旁路，读不到时页面该照常打开并说明原因，
    而不是变成一个 500 错误页。
    """
    fake = _FakeClient(error=LangfuseUnavailableError("Langfuse 接口限流（每分钟 5 次），约 56 秒后自动恢复"))
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: fake)

    result = await trace_service.list_traces(hours=24, limit=10)

    assert result["degraded"] is True
    assert "56" in (result["reason"] or "")
    assert result["traces"] == []


async def test_get_trace_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: _FakeClient(rows=[]))

    with pytest.raises(trace_service.TraceNotFound):
        await trace_service.get_trace("nope")


async def test_get_trace_returns_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        rows=[
            _obs("a", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:01.200Z", type_="AGENT"),
            _obs("b", "agent-plan", parent="a", start="2026-10-20T07:00:00.100Z", end="2026-10-20T07:00:00.600Z", type_="GENERATION"),
        ]
    )
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: fake)

    detail = await trace_service.get_trace("t-1")

    assert detail["degraded"] is False
    assert detail["span_count"] == 2
    assert detail["total_ms"] == pytest.approx(1200, abs=1)
    assert detail["session_id"] == "s-1"
    # 详情必须按 traceId 精确过滤，而不是再按时间窗捞一遍
    assert fake.calls[0]["trace_id"] == "t-1"


# ---------------------------------------------------------------- 提问原文与分类


@pytest.fixture(autouse=True)
def _question_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """
    把「按 trace_id 反查提问」换成内存字典 —— **单测不连库**。

    为什么不让它真查：这些用例要守的是形状与顺序（谁在前、哪条被筛掉），
    不是 SQL 是否正确；真连库会让它们依赖"本机有没有跑 postgres"，
    在 CI 里直接红。SQL 本身由迁移脚本的行为探针覆盖。

    返回值就是那个字典，用例往里塞 `{trace_id: 提问}` 即可。
    """
    store: dict[str, str] = {}

    async def _fake(trace_ids: list[str]) -> dict[str, str]:
        return {tid: store[tid] for tid in trace_ids if tid in store}

    monkeypatch.setattr(trace_service, "_load_questions", _fake)
    return store


def test_kind_of_known_roots() -> None:
    """根 span 名 → kind；不认识的一律 other（不猜、不丢）。"""
    assert trace_service._kind_of("run_agent") == "dialog"
    assert trace_service._kind_of("retriever_inspect") == "inspect"
    assert trace_service._kind_of("eval-judge") == "eval"
    assert trace_service._kind_of("some_new_span") == "other"


def test_pick_questions_keeps_earliest() -> None:
    """
    同一 trace 多条 user 消息时取最早那条。

    None 的 trace_id 必须被丢掉 —— 否则会造出一个以 None 为键的条目，
    后面 `questions.get(trace_id)` 永远取不到，属于"看着有逻辑、其实空转"。
    """
    rows = [
        (None, "没有 trace 的历史消息"),
        ("t1", "原始提问"),
        ("t1", "同一 trace 的第二条（重放）"),
        ("t2", "另一个提问"),
    ]
    assert trace_service._pick_questions(rows) == {"t1": "原始提问", "t2": "另一个提问"}


async def test_filter_happens_before_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    kind 过滤必须发生在**截断之前**。

    这是用户痛点的回归测试：3 条 retriever_inspect + 1 条 run_agent，limit=1。
      · 先过滤后截断 → 得到那条 dialog ✅
      · 先截断后过滤 → 只剩 inspect，筛 dialog 得空 ❌（页面看起来"筛选没反应"）
    实测 7 天窗口里 eval-judge 120 条、inspect 49 条、run_agent 只有 42 条，
    这个顺序错了，对话就会被调试运行整体挤出去。
    """
    rows = [
        _obs("i1", "retriever_inspect", start="2026-10-20T09:00:00.000Z", end="2026-10-20T09:00:01.000Z", trace="ti1"),
        _obs("i2", "retriever_inspect", start="2026-10-20T08:00:00.000Z", end="2026-10-20T08:00:01.000Z", trace="ti2"),
        _obs("i3", "retriever_inspect", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:01.000Z", trace="ti3"),
        _obs("d1", "run_agent", start="2026-10-20T06:00:00.000Z", end="2026-10-20T06:00:02.000Z", trace="td1", type_="AGENT"),
    ]
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: _FakeClient(rows=rows))

    result = await trace_service.list_traces(hours=24, limit=1, kind="dialog")

    assert [t["trace_id"] for t in result["traces"]] == ["td1"]
    # 计数是**过滤前**的：筛选器上要能显示"还有 3 条检索检查"
    assert result["kind_counts"] == {"inspect": 3, "dialog": 1}


async def test_unknown_kind_rejected() -> None:
    """非法 kind 抛 ValueError（接口层把它翻成 400，不是静默返回全部）。"""
    with pytest.raises(ValueError):
        await trace_service.list_traces(hours=24, limit=5, kind="nope")


async def test_list_attaches_question(monkeypatch: pytest.MonkeyPatch, _question_store: dict) -> None:
    """有记录的 trace 带提问原文；没有的必须是 None（不是空串、不是编造）。"""
    _question_store["td1"] = "距离2027年10月1日还有几天？"
    rows = [
        _obs("d1", "run_agent", start="2026-10-20T08:00:00.000Z", end="2026-10-20T08:00:02.000Z", trace="td1", type_="AGENT"),
        _obs("d2", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:02.000Z", trace="td2", type_="AGENT"),
    ]
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: _FakeClient(rows=rows))

    result = await trace_service.list_traces(hours=24, limit=10)

    by_id = {t["trace_id"]: t for t in result["traces"]}
    assert by_id["td1"]["question"] == "距离2027年10月1日还有几天？"
    assert by_id["td2"]["question"] is None, "查不到就是 None —— 页面据此写「未记录提问」"


async def test_detail_carries_kind_and_question(monkeypatch: pytest.MonkeyPatch, _question_store: dict) -> None:
    """详情也要带提问：从列表点进来后，页面上得有个"这是哪次提问"的锚点。"""
    _question_store["t-1"] = "年假有几天？"
    fake = _FakeClient(
        rows=[
            _obs("a", "run_agent", start="2026-10-20T07:00:00.000Z", end="2026-10-20T07:00:01.000Z", type_="AGENT"),
        ]
    )
    monkeypatch.setattr(trace_service, "get_langfuse_client", lambda: fake)

    detail = await trace_service.get_trace("t-1")

    assert detail["kind"] == "dialog"
    assert detail["question"] == "年假有几天？"
