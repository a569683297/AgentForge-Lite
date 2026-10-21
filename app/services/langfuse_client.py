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
  **实测配额：5 次请求 / 分钟**（响应头 `x-ratelimit-limit: 5`、`retry-after: 56`）。

  **当前处理（D37 补充 / 2026-10-19 重做，原方案不够）**：
    ① **不自动重试**，一律抛 `LangfuseUnavailableError` ——
       重试只会加剧限流，而且把"读不到"变成"更久地读不到"；
    ② **负缓存**（`_blocked`）：429 时按响应头的 `retry-after` 记下解封时刻，
       冷却期内直接短路，**一次 HTTP 都不发**。原来的"失败一律不缓存"对网络抖动
       成立，对配额型失败不成立 —— 每次刷新都重打，会把仅剩的额度也打光；
    ③ **单飞**（`_inflight`）：同窗口的并发取数合并成一次。这条是必需的：
       `token_cost` 与 `latency_p95` 的缓存键完全相同，串行时能复用缓存，
       但前端是并发发起的，两个协程会同时 miss、各拉 3 页 —— 6 次请求直接爆掉 5 次配额。
  **减小调用量的手段**（累计）：`query()` 按"单指标 + 时间窗"取数（而非整月拉回再
  本地过滤）+ 60s 缓存 + 单飞合并 + YAML 的 `limit: 300` 控制单次最多 3 页 +
  `POST /api/metrics/overview` 把分析页 6 个查询合成 1 个 HTTP ——
  取舍说明见 `query()` 的 docstring。

  🚩 **仍未解决**：`limit: 300` 只覆盖窗口内最新的 300 条 observation（实测 7 天
  窗口有 3146 条），token 与 P95 都是"前 300 条"的口径，不是全量。
"""

from __future__ import annotations

import asyncio
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
    Langfuse 读通道不可用（网络不通 / 凭据无效 / 返回非 200 / 限流 429）。

    单独一个异常类型，是为了让上层能**只降级这一个指标**（PRD F8.5.4）：
    若混在 RuntimeError 里，调用方分不清"Langfuse 挂了"还是"我的代码写错了" ——
    前者该降级（Agent 该说"该指标暂不可用"），后者该修，处理方式完全相反。

    两个附加字段（D37 补充，2026-10-19）：
        status          HTTP 状态码（网络层失败时为 None）
        retry_after_s   限流时**服务端明说**的冷却秒数（`retry-after` 头）
    为什么冷却时长要跟着异常走、而不是上层写死：见 `_fetch_observations`
    负缓存那段的取舍说明 —— 写死 30 或 60 秒都会和服务端真实窗口错位。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


# ---- 端点（见文件头对 v1/v2 的取舍说明）----
OBSERVATIONS_PATH = "/api/public/observations"

# Trace 页走的端点。**与上面不是一回事**，三处差异决定了它适合"看某一次运行"：
#   ① v1（上面那个）是**指标**用的：5 次/分钟配额，还要拿去算 P95，Trace 页不能挤占；
#   ② v2 是实时路径（官方原话：唯一实时的读路径就是 v2 observations + v2 metrics），
#      而 v1 与 `/api/public/traces` 都有约 10 分钟延迟；
#   ③ v2 支持 `?traceId=` 精确过滤 —— 实测 4 个 span 一次拿全，画一棵树只需 1 次请求。
# 代价：v2 **不带 metadata 与 usageDetails**，所以 Trace 页看不到「工具属于哪台 server」
# 与 token 数。这两项由 /api/metrics 那条链路回答，不在这个页面重复。
V2_OBSERVATIONS_PATH = "/api/public/v2/observations"

# Trace 页单次最多拉多少条 observation。
# 200 条约等于 20~40 次对话（一次 run_agent 出 4~10 个 span），覆盖"最近 24 小时"通常够；
# 触顶时上层会明说"只统计了最近 N 条"，而不是假装完整（与 metrics 的 limit 同一条原则）。
TRACE_FETCH_LIMIT = 200

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


# 限流冷却的兜底秒数：服务端**没给** retry-after 时用它。
# 为什么必须有兜底：没有冷却 = 下一次刷新继续把额度打光。宁可保守挡 30 秒，
# 也不让"越失败越打、越打越失败"的循环继续（实测该端点 429 时一定给 retry-after，
# 这个常量只防代理改写响应头这种边角情况）。
DEFAULT_RETRY_AFTER_S = 30.0


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """
    从 429 响应里取"还要等多少秒"：优先 `retry-after` 头，退回 body 的
    `details.retryAfterSeconds`。

    为什么优先头：`retry-after` 是 HTTP 标准字段，代理/网关改写 body 也动不了它；
    body 那条是 Langfuse 自己的冗余字段（实测两者同时给出同一个值 56）。
    为什么返回 None 而不是默认值：这一层只负责"如实报告服务端说了什么"，
    兜底常量留给调用方决定 —— 数据源层不该替上层做策略选择。
    """
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass  # HTTP-date 格式（本接口不返回）：落到 body 分支

    try:
        details = (resp.json() or {}).get("details") or {}
        value = details.get("retryAfterSeconds")
        if value is not None:
            return max(0.0, float(value))
    except (ValueError, TypeError):
        pass
    return None


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
        # （配额型失败不适用这条理由，单独走下面的 `_blocked` 负缓存。）
        self._cache: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}

        # 单飞（single-flight）：键 → 正在飞的那次取数。
        # 两个指标（token_cost / latency_p95）窗口与 limit 完全相同，缓存键也相同；
        # 但前端是并发发起的，两个协程会**同时** miss 缓存、各拉 3 页 ——
        # 实测 6 次请求超过端点 5 次/分钟的配额，必然有指标被 429 打降级。
        # 有了这张表，第二个调用者直接 await 第一个的 Task，取数只发生一次。
        self._inflight: dict[tuple[str, str, int], asyncio.Task[list[dict]]] = {}

        # 负缓存（negative cache）：键 → (解封时刻, 错误消息)。
        # 「负」指缓存的是**失败**。为什么失败也要缓存：429 是**配额型**失败，
        # 不是网络抖动 —— 响应头 retry-after 明确说了还要等多久，
        # 期间再打一定是同一个 429，只会把额度继续打光。
        # 冷却时长从响应头读（不写死）：写死 30 秒会在第 31 秒撞回同一个窗口，
        # 写死 60 秒会在服务端已放行后白挡 4 秒。
        self._blocked: dict[tuple[str, str, int], tuple[float, str]] = {}

        # ---- v2（Trace 页专用）的**独立**三件套 ----
        # 为什么不复用上面三个字典：v1 的键语义是「时间窗 + limit」，v2 的键可能带
        # traceId。混用同一张表会让"缓存命中"变成巧合，而这类 bug 的表现是
        # 「页面画的树和选中的那条 trace 对不上」—— 不报错，只是错了。
        # 更重要的是：v1 是 metrics 指标的命脉（配额只有 5 次/分钟），
        # Trace 页的高频查询绝不能挤占它，两者必须各自计数、各自冷却。
        self._v2_cache: dict[tuple[str, ...], tuple[float, list[dict]]] = {}
        self._v2_inflight: dict[tuple[str, ...], asyncio.Task[list[dict]]] = {}
        self._v2_blocked: dict[tuple[str, ...], tuple[float, str]] = {}

    # ---------------- 取数 ----------------
    async def _fetch_observations(
        self, from_ts: str, to_ts: str, limit: int = 1000
    ) -> list[dict]:
        """
        按时间窗拉回 observations（负缓存 → 60s 缓存 → 单飞合并 → 真正发 HTTP）。

        四层顺序不能乱，各有分工：
            ① 负缓存（`_blocked`）：上次限流还没解封 → 直接抛，**一次 HTTP 都不发**
            ② 成功缓存（`_cache`）：窗口数据 60s 内不重复拉
            ③ 单飞（`_inflight`）：同一窗口已有取数在飞 → 等它，不另起一次
            ④ `_fetch_pages`：真正分页拉取

        Args:
            from_ts / to_ts: ISO 时间或 `YYYY-MM-DD`（后者会被规整，见 `_to_iso`）
            limit: 最多返回多少条（**不是**每页条数，分页由内部处理）
        Returns:
            原始 observation dict 列表（未做字段映射 —— 那是 `_aggregate` 的事）
        Raises:
            LangfuseUnavailableError: 不可达 / 非 200 / 限流（限流时带 `retry_after_s`）
        """
        key = (from_ts, to_ts, limit)
        now = time.monotonic()

        # ① 负缓存：冷却期内直接短路。
        blocked = self._blocked.get(key)
        if blocked is not None:
            until, message = blocked
            remaining = until - now
            if remaining > 0:
                logger.info(
                    "Langfuse 负缓存命中（限流冷却中）窗口=%s~%s 剩余=%.0fs",
                    from_ts, to_ts, remaining,
                )
                raise LangfuseUnavailableError(
                    message, status=429, retry_after_s=remaining
                )
            self._blocked.pop(key, None)   # 已解封：删掉，往下走正常取数

        # ② 成功缓存
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            logger.info(
                "Langfuse 取数命中缓存 窗口=%s~%s 条数=%d 剩余=%.0fs",
                from_ts, to_ts, len(cached[1]), cached[0] - now,
            )
            return cached[1]

        # ③ 单飞：已有在途取数就搭车，没有才起飞。
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(
                self._fetch_and_cache(key, from_ts, to_ts, limit)
            )
            self._inflight[key] = task
            # 完成后从表里摘掉；`add_done_callback` 保证异常路径也会执行
            # （否则失败一次就会永久占住这个键，后续全部搭上一个已死的 Task）。
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
            logger.info("Langfuse 取数起飞 窗口=%s~%s limit=%d", from_ts, to_ts, limit)
        else:
            logger.info(
                "Langfuse 取数合并到在途请求（单飞）窗口=%s~%s limit=%d",
                from_ts, to_ts, limit,
            )

        # `shield` 必须加：直接 await 共享 Task 时，某个调用者被取消
        # （浏览器断开 / 前端切窗口放弃请求）会把 Task 一起取消，
        # 另一个正常等待的调用者会收到 CancelledError —— 一个用户的取消不该连累另一个。
        return await asyncio.shield(task)

    async def _fetch_and_cache(
        self, key: tuple[str, str, int], from_ts: str, to_ts: str, limit: int
    ) -> list[dict]:
        """
        单飞 Task 的本体：取数 → 成功写缓存 / 429 写负缓存 → 原样抛或返回。

        为什么把"写缓存"放在 Task 里而不是外层 await 之后：
        多个等待者 await 同一个 Task，若写缓存放在外层，就会重复写 N 次
        （虽然无害，但负缓存的解封时刻会被最后一次写覆盖，语义变糊）。
        放在 Task 内部，一个键只写一次，所有等待者共享同一个结果/异常。
        """
        try:
            rows = await self._fetch_pages(from_ts, to_ts, limit)
        except LangfuseUnavailableError as exc:
            # 只有限流才进负缓存：网络抖动/凭据错误缓存了没有意义
            # （网络可能一秒后就恢复，挡 30 秒反而是自伤）。
            if exc.status == 429:
                cooldown = (
                    exc.retry_after_s
                    if exc.retry_after_s is not None
                    else DEFAULT_RETRY_AFTER_S
                )
                self._blocked[key] = (
                    time.monotonic() + cooldown,
                    str(exc),
                )
                logger.warning(
                    "Langfuse 限流，进入冷却 %.0fs（窗口=%s~%s）",
                    cooldown, from_ts, to_ts,
                )
            raise

        self._cache[key] = (time.monotonic() + self._cache_ttl_s, rows)
        return rows

    async def _fetch_pages(self, from_ts: str, to_ts: str, limit: int) -> list[dict]:
        """真正发 HTTP 的部分：按页把结果攒齐。"""
        rows: list[dict] = []
        page = 1
        # 单独计数"实际发了几页"：`page` 在循环末尾会 +1，直接拿它记日志会多报一页
        # （300 条/100 每页 = 实际 3 次请求，但循环结束时 page 已经是 4）——
        # 排障时"日志说 4 页、实际 3 页"会让人白查一轮。
        pages_fetched = 0

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            while len(rows) < limit and page <= self._max_pages:
                params: dict[str, Any] = {
                    "page": page,
                    "limit": min(PAGE_SIZE, limit - len(rows)),
                    "fromStartTime": _to_iso(from_ts),
                    "toStartTime": _to_iso(to_ts, end_of_day=True),
                }
                pages_fetched += 1
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

                if resp.status_code == 429:
                    # 限流单独一条分支：它不是"读通道坏了"，而是"现在不让你读"。
                    # 上层要据此给出**"约 N 秒后恢复"**的人话，所以把 retry-after
                    # 一并带出去（原始英文 body 只进日志 —— 前端展示它是上一版的 bug：
                    # 用户在卡片上看到的就是被截断的 "Rate limit exceeded for GET /ap..."）。
                    retry_after = _parse_retry_after(resp)
                    logger.warning(
                        "Langfuse 限流 429 窗口=%s~%s 第%d页 retry_after=%s 原文=%s",
                        from_ts, to_ts, page, retry_after, resp.text[:200],
                    )
                    wait = (
                        f"约 {retry_after:.0f} 秒后自动恢复"
                        if retry_after is not None
                        else "稍后自动恢复"
                    )
                    raise LangfuseUnavailableError(
                        f"Langfuse 接口限流（每分钟 5 次），{wait}",
                        status=429,
                        retry_after_s=retry_after,
                    )

                if resp.status_code != 200:
                    raise LangfuseUnavailableError(
                        f"Langfuse 返回 {resp.status_code}: {resp.text[:200]}",
                        status=resp.status_code,
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
            from_ts, to_ts, len(rows[:limit]), pages_fetched, limit,
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


    # ---------------- v2 观测：Trace 页的实时路径 ----------------
    async def fetch_v2_observations(
        self,
        *,
        trace_id: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int = TRACE_FETCH_LIMIT,
        cache_ttl_s: float = 20.0,
    ) -> list[dict]:
        """
        拉 v2 observations 明细（**不分页**，一次请求）。

        两种用法，对应 Trace 页的两级视图：
            · 列表：给时间窗，拿回窗口内所有 span，上层按 traceId 自己分组；
            · 详情：给 `trace_id`，一次拿全这棵树的全部节点。

        为什么不翻页：单次 limit 已按「一次页面能画多少」定死（200），
        翻页只会让一次刷新变成 2~3 次请求 —— 而 Trace 页是会被反复点开的。
        触顶时返回条数 == limit，上层据此提示"只统计了最近 N 条"。

        Args:
            trace_id: 精确过滤某一条 trace。传了它就不该再传时间窗（实测可混用，但没有意义）。
            from_ts / to_ts: ISO8601（`2026-10-20T00:00:00Z`）。按 span 的 **startTime** 过滤。
            limit: 最多返回条数。
            cache_ttl_s: 成功结果的缓存时长。为什么比指标的 60s 短：
                         这是"刚刚发生了什么"的视图，缓存太久会让人以为新对话没被埋点。

        Returns:
            原始 observation dict 列表（字段：id / traceId / parentObservationId /
            name / type / startTime / endTime / latency / level / sessionId / modelId）。

        Raises:
            LangfuseUnavailableError: 不可达 / 非 200 / 限流
        """
        key = ("v2", trace_id or "", from_ts or "", to_ts or "", str(limit))
        now = time.monotonic()

        # ① 负缓存：上一次限流还没解封 → 一次 HTTP 都不发。
        blocked = self._v2_blocked.get(key)
        if blocked is not None:
            until, message = blocked
            if until > now:
                raise LangfuseUnavailableError(
                    message, status=429, retry_after_s=until - now
                )
            self._v2_blocked.pop(key, None)

        # ② 成功缓存
        cached = self._v2_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        # ③ 单飞：Trace 页"刷新"按钮很容易被连点，同一窗口的并发合并成一次。
        task = self._v2_inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(
                self._fetch_v2_once(key, trace_id, from_ts, to_ts, limit, cache_ttl_s)
            )
            self._v2_inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._v2_inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _fetch_v2_once(
        self,
        key: tuple[str, ...],
        trace_id: str | None,
        from_ts: str | None,
        to_ts: str | None,
        limit: int,
        cache_ttl_s: float,
    ) -> list[dict]:
        """单飞 Task 的本体：取数 → 成功写缓存 / 429 写负缓存 → 原样抛或返回。"""
        try:
            rows = await self._fetch_v2_pages(
                trace_id=trace_id, from_ts=from_ts, to_ts=to_ts, limit=limit
            )
        except LangfuseUnavailableError as exc:
            if exc.status == 429:
                cooldown = (
                    exc.retry_after_s
                    if exc.retry_after_s is not None
                    else DEFAULT_RETRY_AFTER_S
                )
                self._v2_blocked[key] = (time.monotonic() + cooldown, str(exc))
                logger.warning("Langfuse v2 限流，冷却 %.0fs", cooldown)
            raise

        self._v2_cache[key] = (time.monotonic() + cache_ttl_s, rows)
        logger.info(
            "Langfuse v2 取数完成 trace_id=%s 窗口=%s~%s 条数=%d",
            trace_id or "-", from_ts or "-", to_ts or "-", len(rows),
        )
        return rows

    async def _fetch_v2_pages(
        self,
        *,
        trace_id: str | None,
        from_ts: str | None,
        to_ts: str | None,
        limit: int,
    ) -> list[dict]:
        """真正发 HTTP 的部分（v2 无 totalPages，单次取完）。"""
        params: dict[str, Any] = {"limit": limit}
        if trace_id:
            params["traceId"] = trace_id
        if from_ts:
            params["fromStartTime"] = _to_iso(from_ts)
        if to_ts:
            params["toStartTime"] = _to_iso(to_ts)

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            try:
                resp = await client.get(
                    f"{self._host}{V2_OBSERVATIONS_PATH}",
                    params=params,
                    auth=self._auth,
                )
            except httpx.HTTPError as e:
                raise LangfuseUnavailableError(
                    f"Langfuse 不可达: {type(e).__name__}: {e}"
                ) from e

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            wait = (
                f"约 {retry_after:.0f} 秒后自动恢复"
                if retry_after is not None
                else "稍后自动恢复"
            )
            raise LangfuseUnavailableError(
                f"Langfuse 观测接口限流，{wait}", status=429, retry_after_s=retry_after
            )
        if resp.status_code != 200:
            raise LangfuseUnavailableError(
                f"Langfuse v2 返回 {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )

        return resp.json().get("data") or []


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
