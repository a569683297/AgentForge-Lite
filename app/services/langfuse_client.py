"""
Langfuse 取数通道（D28 / F8.5 / PRD §9.12）
============================================
把 Langfuse 里的**运行追踪数据**（延迟 / token 成本 / 各段耗时）变成 Agent 可分析的对象。

⚠ **名字歧义先说清**（这是本项目最容易混的一处）：
    `app/services/llm_gateway.py` 里的 `langfuse` = **写通道**
        （SDK 的 Langfuse 实例，业务把 span 推上去）
    本文件的 `LangfuseClient`              = **读通道**
        （HTTP 只读，把数据拉回来）
  一个写、一个读，**互不相干、也不共享状态**。两个文件名相似只是巧合。

为什么必须补这条通道（PRD §9.12.1）：
  v4.0 之前只有单向写入（埋点 → Langfuse 面板）。而 Langfuse **有自己的数据库**，
  数据不在项目 PostgreSQL 里 —— 于是延迟 P95、token 成本、各段耗时这些
  **最有运维价值的数据是孤岛**。一句话：**看板是给人看的，回流是给 Agent 用的。**

为什么用 HTTP 而不是 SDK 去读：
  SDK（`Langfuse` 类）是**写**用的，它内部维护着 OTEL TracerProvider 与批量导出线程。
  拿它去读会把"写通道"的实例拖进只读场景，也会让读者误以为读写是一件事。
  F8.5.1 明确要求"封装 `/api/public/observations`" —— 走 REST。

⚠ **两代 REST API 的结构完全不同**（实测，不是照抄 PRD 的接口名就完事）：
    v1  `/api/public/observations`     页码分页（meta.page/totalPages）
                                        **带 usageDetails / metadata / model 名**
    v2  `/api/public/v2/observations`  游标分页（meta.cursor，无 totalPages）
                                        **不带 usageDetails / metadata**
  实测 v1 每次响应都带 `_deprecation`，原文：
    "On Langfuse Cloud, Langfuse v3 is deprecated and this endpoint will be removed
     on November 16, 2026. ... Use GET /api/public/v2/observations instead."
  并且旧版 "may have data delays of several minutes"（新数据可能几分钟才读得到）。

  **为什么当前仍然用 v1**：v2 **拿不到 token 用量**（键集里根本没有 usageDetails），
  而"token 成本"正好是 F8.5 要的一半数据 —— 用 v2 等于这一半永远没有。
  🚩 **未结清**：2026-11-16 前必须重估。届时 token 指标要改走
  `/api/public/v2/metrics`，那是**聚合接口而不是明细接口**，口径会变，
  不是"换一个 URL"那么简单。已登记进 PRD 过时点表。

⚠ **速率限制（实测撞到过，不是理论风险）**：`/api/public/observations` 有 rate limit。
  密集拉取会收到
    `429 {"code":"rate_limited","message":"Rate limit exceeded for GET /api/public/observations.
     Use GET /api/public/v2/observations?... for high-volume reads."}`
  —— 服务端这句话**又一次**指向 v2（与 `_deprecation` 同一个方向）。

  **当前处理：不自动重试，一律抛 `LangfuseUnavailableError`。** 两个理由：
    ① 重试只会加剧限流，而且把"读不到"变成"更久地读不到"；
    ② "这一项暂时不可用"本来就是设计要能表达的状态（PRD F8.5.4 只降级该指标）。
  **减小调用量的手段**（D37 已落地）：`query()` 按"单指标 + 时间窗"取数（而非整月
  拉回再本地过滤）+ 60s 缓存 + YAML 的 `limit: 300` 控制单次最多 3 页 ——
  取舍说明见 `query()` 的 docstring。
"""

from __future__ import annotations

import math
import time
from datetime import timezone
from typing import TYPE_CHECKING, Any

import httpx

from app.config import settings
from app.core.logging import logger

if TYPE_CHECKING:  # 仅为类型注解；运行时不导入（依赖方向保持单向：语义层 → 本站）
    from app.services.metrics_service import MetricSpec


class LangfuseUnavailableError(RuntimeError):
    """
    Langfuse 读通道不可用（网络不通 / 凭据无效 / 返回非 200）。

    单独一个异常类型，是为了让上层能**只降级这一个指标**（PRD F8.5.4）：
    若混在 RuntimeError 里，调用方分不清"Langfuse 挂了"还是"我的代码写错了" ——
    前者该降级（Agent 该说"该指标暂不可用"），后者该修，处理方式完全相反。
    """


# ---- 端点（见文件头对 v1/v2 的取舍说明）----
OBSERVATIONS_PATH = "/api/public/observations"

# 单页条数。实测 v1 单页 limit 上限为 100（传 100 返回 100 条）。
PAGE_SIZE = 100

# 单次取数默认条数上限（语义层 metrics.yaml 的 query.limit 可覆盖；取舍见 query() 说明）。
DEFAULT_OBS_LIMIT = 300

# 字段名映射：语义层用的是 **SDK 侧命名**（`usage.totalTokens`），
# 而 REST 返回的是**另一套键名** —— 这是实测出来的差异，必须显式对照而不是猜。
# （PRD 例子里写的是 `field: latency / usage.totalTokens`，直接拿它去取会静默拿到 None。）
_FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "latency": ("latency",),                              # 秒，float
    "usage.totalTokens": ("usageDetails", "total"),
    "usage.inputTokens": ("usageDetails", "input"),
    "usage.outputTokens": ("usageDetails", "output"),
    "cost.total": ("calculatedTotalCost",),               # 可能为 None（未配价目表）
}

# ---- 聚合方式 / 字段的公开白名单（语义层加载期校验与运行期校验**共用此处**，
#      保证"YAML 里合法的配置"与"运行时跑得动的配置"永远同一份定义）----
SUPPORTED_AGGS: tuple[str, ...] = ("p95", "sum", "avg", "count")
SUPPORTED_FIELDS: frozenset[str] = frozenset(_FIELD_PATHS)


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    """按点号路径取嵌套值；中途断掉返回 None（不抛）。"""
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _to_iso(ts: str, *, end_of_day: bool = False) -> str:
    """
    把语义层给的日期规整成 Langfuse 要的 ISO 时间戳。

    PRD §9.10.2 的接口收的是 `"YYYY-MM-DD"`，而 REST 参数要 ISO（实测
    `fromStartTime=2026-10-04T00:00:00Z` 可用）。只给日期时：
        起点补 T00:00:00Z、终点补 T23:59:59Z（否则 `toStartTime=2026-10-04` 会把
        当天**整天的数据都排除掉** —— 终点被当成当天零点，范围变成空集，
        而返回的是 200 + 空数组，不报错）。

    ⚠ PRD §9.12.4 明确提醒：Langfuse 的时间字段是 `startTime`，
      **不是**项目库里的 `created_at` —— 两个数据源的时间绑定必须分开写。
    """
    ts = (ts or "").strip()
    if not ts:
        raise ValueError("时间参数不能为空")
    if "T" in ts:
        return ts
    return f"{ts}T{'23:59:59' if end_of_day else '00:00:00'}Z"


def _percentile(values: list[float], p: float) -> float:
    """
    最近秩法（nearest-rank）求分位数。

    为什么不用插值法：D35 的 SQL 源会走 `percentile_cont`/`percentile_disc`，
    两个数据源要**同构**（F11.7）。最近秩法取的是"确实存在的那条记录的耗时"，
    与 `percentile_disc` 一致，也比插值值好解释（"第 95 名是 1.83 秒"）。
    ⚠ 样本量很小时（<20）P95 基本等于最大值，报数时要带上样本数 —— 别让它冒充统计结论。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(p / 100.0 * len(ordered)) - 1))
    return ordered[k]


class LangfuseClient:
    """Langfuse **只读**取数客户端。凭据只从 .env 读（PRD §8.2 红线 4）。"""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        cache_ttl_s: float = 60.0,
        timeout_s: float = 30.0,
        max_pages: int = 20,
    ) -> None:
        self._host = (host or "").rstrip("/")
        self._auth = (public_key, secret_key)
        self._cache_ttl_s = cache_ttl_s
        self._timeout_s = timeout_s
        self._max_pages = max_pages
        # 60s 短期缓存（PRD §9.12.4 明确要求）：键是查询窗口，
        # 值 (过期时刻, rows)。只缓存**成功**结果 —— 失败也缓存的话，
        # 一次网络抖动会被放大成"接下来 60 秒都读不到"。
        self._cache: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}

    # ---------------- 取数 ----------------
    async def _fetch_observations(
        self, from_ts: str, to_ts: str, limit: int = 1000
    ) -> list[dict]:
        """
        按时间窗拉回 observations（分页 + 60s 缓存）。

        Args:
            from_ts / to_ts: ISO 时间或 `YYYY-MM-DD`（后者会被规整，见 `_to_iso`）
            limit: 最多返回多少条（**不是**每页条数，分页由内部处理）
        Returns:
            原始 observation dict 列表（未做字段映射 —— 那是 `_aggregate` 的事）
        Raises:
            LangfuseUnavailableError: 不可达 / 非 200
        """
        key = (from_ts, to_ts, limit)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            logger.info(
                "Langfuse 取数命中缓存 窗口=%s~%s 条数=%d 剩余=%.0fs",
                from_ts, to_ts, len(cached[1]), cached[0] - now,
            )
            return cached[1]

        rows = await self._fetch_pages(from_ts, to_ts, limit)
        self._cache[key] = (now + self._cache_ttl_s, rows)
        return rows

    async def _fetch_pages(self, from_ts: str, to_ts: str, limit: int) -> list[dict]:
        """真正发 HTTP 的部分：按页把结果攒齐。"""
        rows: list[dict] = []
        page = 1

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            while len(rows) < limit and page <= self._max_pages:
                params: dict[str, Any] = {
                    "page": page,
                    "limit": min(PAGE_SIZE, limit - len(rows)),
                    "fromStartTime": _to_iso(from_ts),
                    "toStartTime": _to_iso(to_ts, end_of_day=True),
                }
                try:
                    resp = await client.get(
                        f"{self._host}{OBSERVATIONS_PATH}",
                        params=params,
                        auth=self._auth,
                    )
                except httpx.HTTPError as e:
                    # 网络层失败：包装成"读通道不可用"，语义明确（见异常类的说明）
                    raise LangfuseUnavailableError(
                        f"Langfuse 不可达: {type(e).__name__}: {e}"
                    ) from e

                if resp.status_code != 200:
                    raise LangfuseUnavailableError(
                        f"Langfuse 返回 {resp.status_code}: {resp.text[:200]}"
                    )

                data = resp.json()
                batch = data.get("data") or []
                rows.extend(batch)

                if len(batch) < params["limit"]:
                    break    # 最后一页
                total_pages = int((data.get("meta") or {}).get("totalPages") or 1)
                if page >= total_pages:
                    break
                page += 1

        logger.info(
            "Langfuse 取数完成 窗口=%s~%s 条数=%d 页数=%d limit=%d",
            from_ts, to_ts, len(rows[:limit]), page, limit,
        )
        return rows[:limit]

    # ---------------- 聚合 ----------------
    def _aggregate(
        self,
        observations: list[dict],
        *,
        agg: str,
        field: str,
        group_by: str | None = None,
    ) -> list[dict]:
        """
        把明细聚合成"一行一个分组"的表（与 SQL 源的 rows 同构，F11.7）。

        Args:
            observations: `_fetch_observations` 的返回
            agg: `p95` / `sum` / `avg` / `count`
            field: `latency` / `usage.totalTokens` / `usage.inputTokens` /
                   `usage.outputTokens` / `cost.total`（见 `_FIELD_PATHS`）
            group_by: 分组维度（本项目用 `name` —— 就是 span 名，如 `vector_search`）
        Returns:
            `[{"<group_by>": 值 或 None, "value": 数值, "count": 样本数}, ...]`
            ⚠ 行结构的最终定案在语义层（`query()` 会把它归一为 `dim/value/count`，
              与 SQL 源同构，PRD §9.10.5 约束 1）；本函数的输出是**中间形态**，
              分组键名随 `group_by` 传入而变化（不传时为 `"group"`）。
        Raises:
            ValueError: agg 或 field 不在白名单（**不猜、不兜底**）
        """
        if field not in _FIELD_PATHS:
            raise ValueError(
                f"不支持的 field: {field!r}，可选 {sorted(_FIELD_PATHS)}"
            )
        if agg not in SUPPORTED_AGGS:
            raise ValueError(
                f"不支持的 agg: {agg!r}，可选 {' / '.join(SUPPORTED_AGGS)}"
            )

        path = _FIELD_PATHS[field]
        buckets: dict[Any, list[float]] = {}

        for obs in observations:
            raw = _dig(obs, path)
            if raw is None:
                # 缺这项就跳过这条：不能当 0 计入（会把平均值拉低，
                # 表现为"延迟变好了"—— 而真相是"这条根本没数据"）。
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            key = obs.get(group_by) if group_by else None
            buckets.setdefault(key, []).append(value)

        rows: list[dict] = []
        for key, values in buckets.items():
            if agg == "count":
                value = float(len(values))
            elif agg == "sum":
                value = sum(values)
            elif agg == "avg":
                value = sum(values) / len(values)
            else:  # p95
                value = _percentile(values, 95.0)
            rows.append({group_by or "group": key, "value": value, "count": len(values)})

        # 稳定排序：让"同一份数据两次跑出同一行序"，
        # 否则前端 ECharts 的柱子顺序会随机跳（同 D17/D19 的兜底键思路）。
        rows.sort(key=lambda r: (r.get(group_by or "group") is None,
                                 str(r.get(group_by or "group"))))
        return rows

    # ---------------- 对外：语义层出口（D37 / 块 2 实现）----------------
    async def query(
        self,
        spec: "MetricSpec",
        dimension: str | None,
        start: str,
        end: str,
    ) -> list[dict]:
        """
        语义层出口（PRD §9.12.2）：执行一个 `langfuse_api` 指标的取数 + 聚合。

        端到端（在 metrics_service.run_metric 里与 SQL 源对称分派）：
            spec（指标定义）+ dimension + [start, end]
              → ① 维度白名单校验（与 SQL 源**同规则**：换数据源不放松，§9.12.3 约束 4）
              → ② 时间窗换算：北京时间自然日 → UTC ISO
                   （复用 metrics_service.parse_window —— "业务时区"的口径
                    只能有一处实现，两个数据源必须是同一个日界）
              → ③ _fetch_observations（分页 + 60s 缓存）→ _aggregate
              → ④ 归一为语义层行结构 `[{"dim", "value", "count"}, ...]`；
                   无维度时没有 dim 列 —— 与 SQL 源完全同构（§9.10.5 约束 1）

        降级**不在这里做**：不可达 / 限流一律抛 `LangfuseUnavailableError`，
        由上层翻译成 degraded 响应（"只降级该指标"，约束 3，是产品语义；
        数据源层只负责如实报告自己不可用）。

        ⚠ limit 取舍（为什么不是 PRD 例子的 2000）：v1 端点实测限流很紧
          （连续拉 ~5 页就 429），而本系统 1–3 天窗口只有几十~几百条 ——
          300 条（3 页）兼顾"样本足够算 P95"与"远离限流线"。被截断时
          logger 告警，且每行的 count 是**实际样本数**（诚实口径优先于完整口径）。

        Raises:
            MetricsError: 维度不在白名单 / 聚合指标被传了维度（用户参数问题）
            LangfuseUnavailableError: 不可达 / 非 200（系统问题，由上层降级）
        """
        # 延迟导入：本模块与语义层互相引用（语义层分派 → 这里 query；这里借它的
        # parse_window / MetricsError），顶层导入会成环。依赖仍保持单向：
        # 本站不触碰语义层的运行时状态，只复用它的两个纯函数/异常。
        from app.services.metrics_service import MetricsError, parse_window

        # ① 维度白名单（与 render_sql 的规则逐字对齐：白名单原文才放行）
        if spec.dimensions:
            if dimension not in spec.dimensions:
                raise MetricsError(
                    f"指标 {spec.name} 不支持的维度：{dimension!r}；"
                    f"可用维度：{list(spec.dimensions)}"
                )
        elif dimension is not None:
            raise MetricsError(f"指标 {spec.name} 是聚合指标，不支持分组维度：{dimension!r}")

        # ② 查询配置（加载期已校验过合法性与白名单；这里的兜底只为防御性）
        q = spec.query or {}
        agg = str(q.get("agg") or "count")
        field = str(q.get("field") or "latency")
        limit = int(q.get("limit") or DEFAULT_OBS_LIMIT)

        # ③ 时间窗：北京日 [start, end] → UTC ISO 字符串。
        #    parse_window 返回的 end 已是"end 次日 00:00（北京）"（左闭右开），
        #    换算到 UTC 后作为 toStartTime —— 与 SQL 源的边界语义完全一致。
        start_dt, end_dt = parse_window(start, end)
        from_ts = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_ts = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ④ 取数 + 聚合（group_by 用调用方传入的维度；非法值已在 ① 拦下）
        observations = await self._fetch_observations(from_ts, to_ts, limit=limit)
        if len(observations) >= limit:
            logger.warning(
                "Langfuse 指标 %s 取数触顶（%d 条）：窗口内数据可能超过 limit，"
                "聚合口径为**前 N 条**而非全量 —— 确认时看每行 count",
                spec.name, limit,
            )
        raw_rows = self._aggregate(observations, agg=agg, field=field, group_by=dimension)

        # ⑤ 归一为语义层行结构（维度列统一叫 dim；count 样本量保留）
        rows: list[dict] = []
        for r in raw_rows:
            row: dict = {}
            if dimension:
                row["dim"] = r.get(dimension)
            row["value"] = r.get("value")
            row["count"] = r.get("count")
            rows.append(row)

        # 降序（找瓶颈的默认读法，与 tool_usage 的 ORDER BY value DESC 同取向）；
        # dim 作次序键，保证同值时输出稳定（前端柱序不随机跳）
        rows.sort(key=lambda r: (-(r["value"] or 0.0), str(r.get("dim"))))
        return rows


# ---------------- 模块级单例 ----------------
_client: LangfuseClient | None = None


def get_langfuse_client() -> LangfuseClient:
    """
    取读通道单例（凭据**只从 settings 读** —— PRD §8.2 红线 4：业务代码不得直连 os.environ）。

    与 `get_agent()` / `get_embedding_provider()` 同一个模式：工厂 + 缓存。
    """
    global _client
    if _client is None:
        _client = LangfuseClient(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
        )
    return _client
