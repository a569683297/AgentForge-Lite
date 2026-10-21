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

import asyncio

import httpx
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


# ============================================================
# 七、单飞：并发的同窗口读取只落一次 HTTP（D37 补充 / 2026-10-19）
# ============================================================
async def test_concurrent_reads_share_single_flight(monkeypatch):
    """两个指标（token_cost / latency_p95）窗口与 limit 相同，缓存键也相同。

    原来的写法在**串行**时能复用缓存，但前端 Promise.all 是并发发起的 ——
    两个协程同时 miss、各拉 3 页，6 次请求直接超过 Langfuse 的 5 次/分钟配额。
    这条用例钉住"并发也只取一次"。
    """
    calls: list[str] = []

    async def fake_pages(from_ts: str, to_ts: str, limit: int):
        calls.append(from_ts)
        await asyncio.sleep(0.05)      # 模拟真实 HTTP 飞行时间（让第二个调用者能搭车）
        return [{"name": "run_agent", "latency": 1.0}]

    client = _client()
    monkeypatch.setattr(client, "_fetch_pages", fake_pages)

    first, second = await asyncio.gather(
        client._fetch_observations("2026-10-15", "2026-10-16", limit=300),
        client._fetch_observations("2026-10-15", "2026-10-16", limit=300),
    )

    assert len(calls) == 1, "并发的同窗口读取必须合并成一次取数"
    assert first == second, "两个调用者拿到同一个结果（聚合口径不同，但原始明细是同一份）"
    assert client._inflight == {}, "取数完成后在途表必须清空，否则下次会搭上一个已死的 Task"


async def test_single_flight_propagates_failure_to_all_waiters(monkeypatch):
    """共享 Task 的异常要同时传给两个等待者，且失败后不能在途表里留下死 Task。"""

    async def fake_pages(from_ts: str, to_ts: str, limit: int):
        await asyncio.sleep(0.02)
        raise LangfuseUnavailableError("Langfuse 返回 500: boom", status=500)

    client = _client()
    monkeypatch.setattr(client, "_fetch_pages", fake_pages)

    results = await asyncio.gather(
        client._fetch_observations("2026-10-15", "2026-10-16", limit=300),
        client._fetch_observations("2026-10-15", "2026-10-16", limit=300),
        return_exceptions=True,
    )

    assert all(isinstance(r, LangfuseUnavailableError) for r in results)
    assert client._inflight == {}


# ============================================================
# 八、负缓存：429 之后冷却期内不再发请求（D37 补充 / 2026-10-19）
# ============================================================
async def test_rate_limit_opens_cooldown_and_short_circuits(monkeypatch):
    """429 是**配额型**失败，不是网络抖动：retry-after 期间再打一定是同一个 429。

    第一次调用抛错并写入冷却；第二次调用必须**一次 HTTP 都不发**，
    且异常里仍带着剩余冷却秒数（前端据此显示"约 N 秒后恢复"）。
    """
    calls: list[int] = []

    async def fake_pages(from_ts: str, to_ts: str, limit: int):
        calls.append(1)
        raise LangfuseUnavailableError(
            "Langfuse 接口限流（每分钟 5 次），约 56 秒后自动恢复",
            status=429,
            retry_after_s=56.0,
        )

    client = _client()
    monkeypatch.setattr(client, "_fetch_pages", fake_pages)

    with pytest.raises(LangfuseUnavailableError):
        await client._fetch_observations("2026-10-15", "2026-10-16", limit=300)

    with pytest.raises(LangfuseUnavailableError) as exc:
        await client._fetch_observations("2026-10-15", "2026-10-16", limit=300)

    assert len(calls) == 1, "冷却期内必须短路，不能再打一次 HTTP"
    remaining = exc.value.retry_after_s or 0.0
    assert 0 < remaining <= 56.0
    assert "限流" in str(exc.value), "错误消息必须是给人看的中文，而不是 Langfuse 英文原文"


async def test_non_429_failure_is_not_negatively_cached(monkeypatch):
    """只有 429 进负缓存：网络抖动/5xx 一秒后可能就恢复了，挡 30 秒是自伤。"""
    calls: list[int] = []

    async def fake_pages(from_ts: str, to_ts: str, limit: int):
        calls.append(1)
        raise LangfuseUnavailableError("Langfuse 不可达: ConnectError", status=None)

    client = _client()
    monkeypatch.setattr(client, "_fetch_pages", fake_pages)

    for _ in range(2):
        with pytest.raises(LangfuseUnavailableError):
            await client._fetch_observations("2026-10-15", "2026-10-16", limit=300)

    assert len(calls) == 2, "非限流失败必须允许下一次重试"


async def test_cooldown_expires_and_allows_new_attempt(monkeypatch):
    """冷却到期后要能重新取数，而不是永久拉黑（用极短 retry-after 验证解封逻辑）。"""
    calls: list[int] = []

    async def fake_pages(from_ts: str, to_ts: str, limit: int):
        calls.append(1)
        if len(calls) == 1:
            raise LangfuseUnavailableError(
                "Langfuse 接口限流，约 0.05 秒后自动恢复", status=429, retry_after_s=0.05
            )
        return [{"name": "run_agent", "latency": 2.0}]

    client = _client()
    monkeypatch.setattr(client, "_fetch_pages", fake_pages)

    with pytest.raises(LangfuseUnavailableError):
        await client._fetch_observations("2026-10-15", "2026-10-16", limit=300)

    await asyncio.sleep(0.06)          # 等冷却过去
    rows = await client._fetch_observations("2026-10-15", "2026-10-16", limit=300)

    assert rows == [{"name": "run_agent", "latency": 2.0}]
    assert len(calls) == 2


# ============================================================
# 九、retry-after 解析（冷却时长必须来自服务端，不能写死）
# ============================================================
def test_parse_retry_after_prefers_header():
    resp = httpx.Response(
        429,
        headers={"retry-after": "56"},
        json={"details": {"retryAfterSeconds": 99}},   # body 与头不一致时以头为准
    )
    assert lfc._parse_retry_after(resp) == 56.0


def test_parse_retry_after_falls_back_to_body():
    resp = httpx.Response(429, json={"details": {"retryAfterSeconds": 42}})
    assert lfc._parse_retry_after(resp) == 42.0


def test_parse_retry_after_returns_none_when_absent():
    """两个来源都没有时返回 None（由调用方决定兜底值）——数据源层不做策略选择。"""
    assert lfc._parse_retry_after(httpx.Response(429, text="too many requests")) is None
