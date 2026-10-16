"""
单元：Langfuse 源指标（query() 逻辑 / 降级翻译 / 缓存复用）
============================================================
不碰外网：HTTP 层用 monkeypatch 替换；唯一"真实网络行为"是连接本机被拒的
端口（127.0.0.1:9），它验证的正是"httpx 异常 → LangfuseUnavailableError 包装"
这条链路 —— 本机端口拒绝是确定性的，不依赖任何外部服务。

为什么这组用例必须存在（对照 PRD §9.10.5 四条约束逐条钉住）：
  · 约束 1（结构一致）—— 归一后的行结构（dim / value / count）逐字段断言；
  · 约束 3（只降级该指标）—— run_metric 不抛错、返回 degraded 结构；
  · 约束 4（维度白名单不放松）—— 与 sql 源同规则的拒绝路径；
  · 时区口径 —— Langfuse 时间窗必须是**北京时间自然日**（与 SQL 源同一日界；
    "差 8 小时的静默错位"在这里被钉死，而不是等对账时才发现）。
"""

import pytest

from app.services import langfuse_client as lfc
from app.services import metrics_service as ms
from app.services.langfuse_client import LangfuseClient, LangfuseUnavailableError


def _client() -> LangfuseClient:
    """构造一个不会真的发请求的客户端（_fetch 都会被 monkeypatch 替换）。"""
    return LangfuseClient(host="http://unused", public_key="k", secret_key="s")


def _fake_fetch(
    rows: list[dict],
    captured: dict | None = None,
):
    """造一个假的 _fetch_observations：吞参数（可选记录）、吐固定行。"""

    async def fake_fetch(from_ts: str, to_ts: str, limit: int = 1000):
        if captured is not None:
            captured.update(from_ts=from_ts, to_ts=to_ts, limit=limit)
        return rows

    return fake_fetch


# ============================================================
# 一、YAML 配置契约（两个指标的声明逐字段钉住）
# ============================================================
def test_langfuse_metric_specs_declared_in_yaml():
    lat = ms.METRIC_REGISTRY["latency_p95"]
    assert lat.source == "langfuse_api"
    assert lat.dimensions == ("name",)
    assert lat.query == {"agg": "p95", "field": "latency", "group_by": "name", "limit": 300}

    tok = ms.METRIC_REGISTRY["token_cost"]
    assert tok.source == "langfuse_api"
    assert tok.dimensions == ("model",)
    assert tok.query == {
        "agg": "sum",
        "field": "usage.totalTokens",
        "group_by": "model",
        "limit": 300,
    }


# ============================================================
# 二、时间窗：北京时间自然日 → UTC ISO（与 SQL 源同一日界）
# ============================================================
async def test_query_converts_window_to_utc(monkeypatch):
    """单日窗口 2026-10-15（北京）→ UTC [10-14T16:00Z, 10-15T16:00Z)。

    换算正确性 = 北京时间 00:00 == 前一日 UTC 16:00；右界是"次日 00:00（北京）"
    （左闭右开），与 parse_window 给 SQL 源的边界完全一致。
    """
    captured: dict = {}
    client = _client()
    monkeypatch.setattr(client, "_fetch_observations", _fake_fetch([], captured))

    rows = await client.query(
        ms.METRIC_REGISTRY["latency_p95"], "name", "2026-10-15", "2026-10-15"
    )

    assert captured["from_ts"] == "2026-10-14T16:00:00Z"
    assert captured["to_ts"] == "2026-10-15T16:00:00Z"
    assert captured["limit"] == 300          # YAML 的 limit 透传（PRD 2000 → 实测收紧）
    assert rows == []


# ============================================================
# 三、聚合与归一：p95 / sum、dim 列、降序、样本数
# ============================================================
async def test_query_aggregates_p95_and_normalizes(monkeypatch):
    """P95（最近秩法）：3 样本时 = 最大值；行结构 = dim/value/count，按 value 降序。"""
    rows_in = [
        {"name": "run_agent", "latency": 2.0},
        {"name": "run_agent", "latency": 2.5},
        {"name": "run_agent", "latency": 3.0},
        {"name": "bm25", "latency": 0.2},
        {"name": "bm25", "latency": 0.4},
    ]
    client = _client()
    monkeypatch.setattr(client, "_fetch_observations", _fake_fetch(rows_in))

    rows = await client.query(
        ms.METRIC_REGISTRY["latency_p95"], "name", "2026-10-15", "2026-10-15"
    )

    assert rows == [
        {"dim": "run_agent", "value": 3.0, "count": 3},
        {"dim": "bm25", "value": 0.4, "count": 2},
    ]


async def test_query_sums_usage_tokens(monkeypatch):
    """token_cost：usage.totalTokens 求和；不同模型各自成行。"""
    rows_in = [
        {"model": "deepseek-chat", "usageDetails": {"total": 100, "input": 90, "output": 10}},
        {"model": "deepseek-chat", "usageDetails": {"total": 50}},
        {"model": "local-qwen", "usageDetails": {"total": 7}},
    ]
    client = _client()
    monkeypatch.setattr(client, "_fetch_observations", _fake_fetch(rows_in))

    rows = await client.query(
        ms.METRIC_REGISTRY["token_cost"], "model", "2026-10-15", "2026-10-15"
    )

    assert rows == [
        {"dim": "deepseek-chat", "value": 150.0, "count": 2},
        {"dim": "local-qwen", "value": 7.0, "count": 1},
    ]


async def test_query_skips_rows_without_field(monkeypatch):
    """缺字段/None 的行**跳过**而不是按 0 计入 —— 否则平均值/求和会被静默拉低。"""
    rows_in = [
        {"name": "run_agent", "latency": 2.0},
        {"name": "run_agent", "latency": None},
        {"name": "run_agent"},
        {"name": "bm25", "latency": "not-a-number"},   # 脏数据同样跳过
    ]
    client = _client()
    monkeypatch.setattr(client, "_fetch_observations", _fake_fetch(rows_in))

    rows = await client.query(
        ms.METRIC_REGISTRY["latency_p95"], "name", "2026-10-15", "2026-10-15"
    )

    assert rows == [{"dim": "run_agent", "value": 2.0, "count": 1}]


# ============================================================
# 四、维度白名单（约束 4：与 SQL 源同规则，换数据源不放松）
# ============================================================
async def test_query_rejects_dimension_outside_whitelist():
    client = _client()
    with pytest.raises(ms.MetricsError, match="不支持的维度") as exc:
        await client.query(
            ms.METRIC_REGISTRY["latency_p95"], "model", "2026-10-15", "2026-10-15"
        )
    assert "name" in str(exc.value)          # 错误消息列出可用维度（给 LLM 的纠错提示）


async def test_query_rejects_missing_dimension_for_dimensioned_metric():
    """声明了维度的指标不传维度 → 拒绝（与 SQL 源 render_sql 同一条规则）。"""
    client = _client()
    with pytest.raises(ms.MetricsError, match="不支持的维度"):
        await client.query(
            ms.METRIC_REGISTRY["latency_p95"], None, "2026-10-15", "2026-10-15"
        )


async def test_query_rejects_dimension_on_aggregate_metric():
    """聚合指标（dimensions 为空）被传维度 → 拒绝并说明该指标不支持分组。"""
    agg_spec = ms.MetricSpec(
        name="agg_probe",
        label="探针",
        description="测试用聚合指标",
        source="langfuse_api",
        dimensions=(),
        endpoint="/api/public/observations",
        query={"agg": "count", "field": "latency"},
    )
    client = _client()
    with pytest.raises(ms.MetricsError, match="聚合指标"):
        await client.query(agg_spec, "name", "2026-10-15", "2026-10-15")


# ============================================================
# 五、降级（约束 3：Langfuse 不可达 → 只降级该指标，不抛错）
# ============================================================
async def test_run_metric_degrades_when_langfuse_unavailable(monkeypatch):
    """读通道抛 LangfuseUnavailableError 时：HTTP 语义上是"正常返回"，
    结构里 degraded=True + error 说明 + rows=[] —— 调用方按标记行事。"""

    class _UnavailableStub:
        async def query(self, *args, **kwargs):
            raise LangfuseUnavailableError("Langfuse 不可达: ConnectError（模拟）")

    monkeypatch.setattr(lfc, "_client", _UnavailableStub())

    result = await ms.run_metric("latency_p95", "name", "2026-10-15", "2026-10-16")

    assert result["source"] == "langfuse_api"
    assert result["degraded"] is True
    assert result["rows"] == []
    assert "不可达" in result["error"]


async def test_unreachable_host_wrapped_as_unavailable():
    """真实走一遍 HTTP 层：不可达的 host 必须被包装成 LangfuseUnavailableError
    （上层降级的唯一触发条件就是这个类型）。

    ⚠ 实测：本机环境有出站代理，连 127.0.0.1:9 的请求会以两种形态之一失败：
      · 直连拒绝 → httpx.ConnectError → "Langfuse 不可达: ConnectError..."
      · 代理拦截 → HTTP 502 → "Langfuse 返回 502: ..."
    两条路径**都必须**汇入同一异常类型 —— 测试只钉这个契约，
    不钉具体消息（钉死消息 = 换个网络环境就红）。
    """
    client = LangfuseClient(
        host="http://127.0.0.1:9",       # discard 端口：本机必然拒绝连接
        public_key="k",
        secret_key="s",
        timeout_s=1.0,
    )
    with pytest.raises(LangfuseUnavailableError) as exc:
        await client.query(
            ms.METRIC_REGISTRY["latency_p95"], "name", "2026-10-15", "2026-10-15"
        )
    assert "Langfuse" in str(exc.value)


# ============================================================
# 六、缓存复用（限流对策：同窗口二次读取不再发 HTTP）
# ============================================================
async def test_fetch_observations_reuses_cache(monkeypatch):
    calls: list[tuple] = []

    async def fake_pages(from_ts: str, to_ts: str, limit: int):
        calls.append((from_ts, to_ts, limit))
        return [{"name": "run_agent", "latency": 1.0}]

    client = _client()
    monkeypatch.setattr(client, "_fetch_pages", fake_pages)

    first = await client._fetch_observations("2026-10-15", "2026-10-16", limit=100)
    second = await client._fetch_observations("2026-10-15", "2026-10-16", limit=100)

    assert len(calls) == 1, "同窗口同 limit 的第二次读取应命中缓存、不再发请求"
    assert first is second
