"""
Trace 查询服务：把 Langfuse 的 span 明细还原成「一条 trace 的树」
==================================================================
职责边界很窄：**只做形状转换**（扁平明细 → 父子树 + 时间偏移），不做取数、
不做降级判断之外的事。取数在 `langfuse_client.fetch_v2_observations`，
降级后的统一响应形状由本模块给出（与 `metrics_service` 的 degraded 同风格）。

为什么要自己建树，而不是直接用 `/api/public/traces`：
  · 那个端点一次就返回整棵树，但它 meta 里写着 `sunsetAt: 2026-11-16`，
    与 v1 observations 同一天下线；
  · 它还有约 10 分钟延迟（官方文档：只有 v2 observations / v2 metrics 是实时路径）。
  → 用 v2 按 `traceId` 拉明细自己拼，既不踩下线，也不会"刚聊完看不到"。

一个必须说清的口径：**时间偏移以「本次 trace 内最早的 span」为 0 点**。
所以瀑布图的横轴是"这一次运行内部"的相对时间，不是墙上时间。
跨窗口边界的 trace（例如 23:59:58 开始、00:00:03 结束）在列表里可能只拿到后半截 ——
表现为 root 缺失、`span_count` 偏小，而不是报错。这是"按时间窗取数"的固有代价。

**「一次提问」的边界（2026-10-20 补）**：
  一条 trace = 一次 `run_agent` 调用 = 用户按一次发送。所以「按 trace 分组」
  本身就是「按提问分组」—— 这一层原本就是对的。
  真正缺的是**提问原文**：v2 observations 不返回 input（实测键集里没有），
  所以过去页面上只剩 `run_agent` + 时间，N 次提问看起来一模一样。
  现在用 `messages.trace_id` 反查（见 `_load_questions`），列表才能写成
  "15:24 · 距离2027年10月1日还有几天？"。

关于 `kind`（为什么需要它）：
  同一个 Langfuse 项目里混着三类运行 —— 真实对话（run_agent）、
  检索检查器的手工查询（retriever_inspect）、评测判分（eval-judge）。
  实测 7 天窗口：eval-judge 120 条、retriever_inspect 49 条、run_agent 42 条。
  按时间取前 20 条时，**对话会被调试运行挤出去**（实测某次 24h 窗口
  前 200 个 span 里 77 条 trace 全是 retriever_inspect）。
  所以列表必须能先按类型过滤、再截断 —— 顺序反了等于没过滤。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.message import Message
from app.services.langfuse_client import (
    TRACE_FETCH_LIMIT,
    LangfuseUnavailableError,
    get_langfuse_client,
)
from app.services.observability import SPAN_INSPECT, SPAN_ROOT


class TraceNotFound(LookupError):
    """trace 不存在，或已超出 Langfuse 的保留期（Cloud 免费档通常是 30 天）。"""


# ---- trace 分类（kind）----
# 键是**根 span 名** —— 这个映射只能有一份：接口层用它校验筛选参数，
# 前端用它翻译成中文。就地再写一份字符串，改一处就会静默漏掉一类。
KIND_BY_ROOT: dict[str, str] = {
    SPAN_ROOT: "dialog",            # run_agent   → 真实对话（每次提问一条）
    SPAN_INSPECT: "inspect",        # retriever_inspect → 检索检查器的手工查询
    # 字面量而非常量：judge_service 里的 `trace_name="eval-judge"` 是唯一出处，
    # 但那是 trace **写入**侧的命名，这里只是**读取**侧的识别 —— 从写入侧 import
    # 会让"读 Trace" 反过来依赖"做评测"，没有必要。改名时两处一起改（grep 得到）。
    "eval-judge": "eval",
}
KINDS: tuple[str, ...] = ("dialog", "inspect", "eval", "other")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _kind_of(root_name: str) -> str:
    """根 span 名 → kind；不认识的一律归 other（不猜、不丢）。"""
    return KIND_BY_ROOT.get(root_name, "other")



def _parse_ts(value: Any) -> datetime | None:
    """解析 Langfuse 的 ISO 时间（形如 `2026-10-20T07:24:50.163Z`）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span_end(obs: dict) -> datetime | None:
    """
    一个 span 的结束时刻。

    优先用 `endTime`；没有时退回 `startTime + latency` —— 后者是 Langfuse 自己算的
    耗时（秒），两者在实测样本里一致到毫秒。都用不了就返回 None，
    上层会退化成"零长度条"，而不是崩掉。
    """
    end = _parse_ts(obs.get("endTime"))
    if end is not None:
        return end
    start = _parse_ts(obs.get("startTime"))
    latency = obs.get("latency")
    if start is not None and isinstance(latency, (int, float)):
        return start + timedelta(seconds=float(latency))
    return start


def _sort_time(obs: dict) -> datetime:
    return _parse_ts(obs.get("startTime")) or _EPOCH


def _has_error(spans: list[dict]) -> bool:
    """
    这条 trace 里有没有**真正的错误**。

    警告不算（例如"重排超时降级"）—— 理由见 `_status_of`：
    降级是"这次没精排"，不是"这次失败了"。混进来会让列表上的红点失去意义，
    真正出错的 trace 反而被淹没在一片红点里。
    """
    return any((span.get("level") or "").upper() == "ERROR" for span in spans)


def _status_of(span: dict) -> str:
    """
    单个 span 的状态：`ok` / `warning` / `error`。

    为什么必须把 warning 从 error 里拆出来（这是接上真实数据后才暴露的误导）：
      重排降级时写的是 `level="WARNING"` + `status_message="重排超时降级（返回融合序）"`，
      而旧逻辑是"有 statusMessage 就算 error" —— 于是一次**降级**在瀑布图上
      被染成和 500 错误一样的红色。实测复现：某次对话 rerank 3006ms 超时降级，
      整条 trace 看起来"重排挂了"，而实际上只是退回了融合序。
      两件事的处置完全不同：降级要调超时/缩候选池，错误要去查堆栈。
    """
    level = (span.get("level") or "").upper()
    if level == "ERROR":
        return "error"
    if level == "WARNING" or span.get("statusMessage"):
        return "warning"
    return "ok"


def _duration_ms(obs: dict[str, Any]) -> float:
    """单个 span 的耗时（毫秒）。优先 Langfuse 算好的 latency，其次用首尾时间差。"""
    latency = obs.get("latency")
    if isinstance(latency, (int, float)):
        return round(float(latency) * 1000, 1)
    start, end = _parse_ts(obs.get("startTime")), _span_end(obs)
    if start is not None and end is not None:
        return round((end - start).total_seconds() * 1000, 1)
    return 0.0


def _summarize(trace_id: str, spans: list[dict]) -> dict[str, Any]:
    """把同一 trace 的 span 列表压成一行摘要（列表页用，不建树）。"""
    starts = [ts for ts in (_parse_ts(s.get("startTime")) for s in spans) if ts]
    ends = [ts for ts in (_span_end(s) for s in spans) if ts]
    start = min(starts) if starts else _EPOCH
    end = max(ends) if ends else start

    # 根 span：没有父的那一个。拿不到根（被时间窗截断）就退而取最早的一个 ——
    # 名字只是给人认的，不该因为截断而变成空字符串。
    roots = [s for s in spans if not s.get("parentObservationId")]
    head = roots[0] if roots else min(spans, key=_sort_time)
    head_name = head.get("name") or ""

    # sessionId 在 v2 里每个 span 都带，取第一个非空即可
    session_id = next((s.get("sessionId") for s in spans if s.get("sessionId")), None)

    return {
        "trace_id": trace_id,
        "name": head_name or "(未命名)",
        # kind 用**根 span 名**判定，而不是"里面有没有某类子 span"：
        # 一次对话里可能也调了检索（retrieval 子 span），按子 span 判会把它
        # 同时算进 inspect，筛选就失效了。
        "kind": _kind_of(head_name),
        "session_id": session_id,
        "start_time": start,
        "duration_ms": round((end - start).total_seconds() * 1000, 1),
        "span_count": len(spans),
        "has_error": _has_error(spans),
    }


def _pick_questions(rows: list[tuple[str | None, str]]) -> dict[str, str]:
    """
    `(trace_id, content)` 行 → `{trace_id: 提问文本}`（纯函数，便于单测）。

    同一 trace 有多条 user 消息时**先到先得**（调用方按 messages.id 升序给行）：
    正常情况下不会发生（一次提问 = 一条 user 消息 = 一条 trace），
    但真出现重放/补发时，最早那条才是原始提问。
    """
    picked: dict[str, str] = {}
    for trace_id, content in rows:
        if trace_id and trace_id not in picked:
            picked[trace_id] = content
    return picked


async def _load_questions(trace_ids: list[str]) -> dict[str, str]:
    """
    按 trace_id 反查「那次提问问了什么」。**查自己的 messages 表，不查 Langfuse。**

    为什么必须走自己的库：v2 observations 实测不返回 input（键集里没有这个字段），
    而 v1 的 `/api/public/traces` 虽然带 input，却 2026-11-16 下线、且一次只能查一条。
    项目在落库时已把 trace id 写到那条 user 消息上（`append_messages(trace_id=...)`），
    这里只是批量取回 —— 一次 SQL、零额外配额、不依赖任何会下线的端点。

    查不到是**正常状态**，不是错误：
      · 本次改造之前的历史对话没有 trace_id；
      · 评测模式（persist=False）根本不落库。
    两种情况都返回空，页面显示"未记录提问"，不编造。

    空列表提前返回：`= any('{}')` 在 PostgreSQL 上会因无法推断数组类型而报错，
    不是"空集合恒假"那么无害（D23 踩过同一个坑）。
    """
    if not trace_ids:
        return {}

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Message.trace_id, Message.content)
                .where(Message.trace_id.in_(trace_ids), Message.role == "user")
                .order_by(Message.id)
            )
        ).all()

    return _pick_questions([(row.trace_id, row.content) for row in rows])


def _build_spans(spans: list[dict]) -> tuple[list[dict], float]:
    """
    扁平 span 列表 → 先序排列的树（带 depth 与相对时间）。

    返回 (节点列表, 总耗时毫秒)。节点顺序就是渲染顺序：父一定排在子前面，
    前端按数组顺序逐行画、用 depth 做缩进即可，不需要递归。

    树不完整时（父节点被 limit 截掉）**把孤儿当根**，不丢弃 ——
    丢掉会让"明明有 6 个 span，页面只画 4 个"，而且不报错。
    """
    by_id: dict[str, dict] = {}
    for span in spans:
        span_id = span.get("id")
        if span_id:
            by_id[span_id] = span

    base = min((_sort_time(s) for s in spans), default=_EPOCH)
    total_ms = max(
        ((_span_end(s) - base).total_seconds() * 1000 for s in spans if _span_end(s)),
        default=0.0,
    )

    children: dict[str, list[dict]] = defaultdict(list)
    roots: list[dict] = []
    for span in spans:
        parent = span.get("parentObservationId")
        if parent and parent in by_id:
            children[parent].append(span)
        else:
            roots.append(span)

    ordered: list[dict] = []

    def walk(span: dict, depth: int) -> None:
        start = _parse_ts(span.get("startTime")) or base
        end = _span_end(span) or start
        duration = _duration_ms(span)
        ordered.append(
            {
                "id": span.get("id") or "",
                "parent_id": span.get("parentObservationId"),
                "name": span.get("name") or "(未命名)",
                "type": (span.get("type") or "SPAN").upper(),
                "depth": depth,
                "start_ms": round((start - base).total_seconds() * 1000, 1),
                "end_ms": round((end - base).total_seconds() * 1000, 1),
                "duration_ms": duration,
                "status": _status_of(span),
                "status_message": span.get("statusMessage") or None,
                "model": span.get("modelId"),
            }
        )
        # 同一层内按开始时间排：与 Langfuse 面板的读法一致，
        # 也让"两次刷新顺序不同"这种抖动不会出现
        for child in sorted(children.get(span.get("id") or "", []), key=_sort_time):
            walk(child, depth + 1)

    for root in sorted(roots, key=_sort_time):
        walk(root, 0)

    return ordered, round(total_ms, 1)


async def list_traces(
    *, hours: int = 24, limit: int = 20, kind: str | None = None
) -> dict[str, Any]:
    """
    最近的 trace 列表（按开始时间倒序）。

    端到端：给定小时数 → 换成 UTC 窗口 → 拉 v2 明细 → 按 traceId 分组 →
    每组压成一行摘要 → **按 kind 过滤** → 倒序取前 `limit` 条 → 反查提问原文。

    ⚠ 过滤必须在截断**之前**。反过来的话，"只对话"筛出来的仍是那 20 条里
    恰好是对话的几条 —— 而实际情况是 7 天里 120 条 eval-judge + 49 条
    retriever_inspect 会把 42 条 run_agent 几乎全挤出去，筛选器看起来"没反应"。

    降级**在这里收敛**：Langfuse 不可达/限流时返回 `degraded=true` + 人话原因，
    HTTP 仍是 200。理由与 metrics 一致 —— "观测数据现在读不到"不是一个
    让整页报错的错误，页面该照常打开并说明原因。

    Args:
        hours: 回看窗口（小时）。上限由接口层限制，这里不再校验。
        limit: 最多返回多少条 trace（注意与 `TRACE_FETCH_LIMIT` 不是一回事：
               后者限制**span**条数，前者限制**对话**条数）。
        kind: `dialog` / `inspect` / `eval` / `other`；None = 全部。
    Raises:
        ValueError: kind 不在 `KINDS` 里（接口层已用 Literal 拦过，这里防御）
    """
    if kind is not None and kind not in KINDS:
        raise ValueError(f"未知 trace 类型：{kind!r}，可选 {list(KINDS)}")

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)

    try:
        rows = await get_langfuse_client().fetch_v2_observations(
            from_ts=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            to_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            limit=TRACE_FETCH_LIMIT,
        )
    except LangfuseUnavailableError as exc:
        logger.warning("Trace 列表降级：%s", exc)
        return {
            "degraded": True,
            "reason": str(exc),
            "window_hours": hours,
            "fetched_at": now,
            "truncated": False,
            "kind_counts": {},
            "traces": [],
        }

    grouped: dict[str, list[dict]] = defaultdict(list)
    for obs in rows:
        trace_id = obs.get("traceId")
        if trace_id:
            grouped[trace_id].append(obs)

    traces = [_summarize(trace_id, spans) for trace_id, spans in grouped.items()]

    # 分类计数在**过滤前**算：它是筛选器上"有 3 条对话 / 17 条检索检查"的唯一依据。
    # 过滤后算就永远只剩当前选中的那一类，用户看不到"别的类还有多少"。
    kind_counts = dict(Counter(trace["kind"] for trace in traces))

    if kind is not None:
        traces = [trace for trace in traces if trace["kind"] == kind]

    traces.sort(key=lambda t: t["start_time"], reverse=True)
    traces = traces[:limit]

    # 反查提问原文：只查**最终要返回的这几条**，不做全量（20 条 vs 几百条）
    trace_ids = [trace["trace_id"] for trace in traces]
    try:
        questions = await _load_questions(trace_ids)
    except Exception as exc:  # noqa: BLE001 —— 见下方说明
        # 提问原文是**增强信息**：DB 暂时读不到时，时间线本身仍然可用。
        # 让它把整页打成 500 是本末倒置（与"观测读不到只降级"同一条原则）。
        # 不带堆栈：DB 挂掉时每次刷新都会走这里，堆栈会淹掉真正的故障日志。
        logger.warning("按 trace_id 反查提问失败（时间线仍可看）：%s", exc)
        questions = {}

    for trace in traces:
        trace["question"] = questions.get(trace["trace_id"])

    return {
        "degraded": False,
        "reason": None,
        "window_hours": hours,
        "fetched_at": now,
        # 触顶只说明"这一窗内 span 太多，可能没取全"，不是错误
        "truncated": len(rows) >= TRACE_FETCH_LIMIT,
        # 只含有值的类；前端按 KINDS 兜底 0（少传一个键比传错值安全）
        "kind_counts": kind_counts,
        "traces": traces,
    }


async def get_trace(trace_id: str) -> dict[str, Any]:
    """
    一条 trace 的完整 span 树（先序，带 depth 与相对毫秒）。

    一次请求拿全：v2 支持 `?traceId=` 精确过滤，实测一条 run_agent 的 4 个 span
    一次返回。所以详情页不需要"先查列表再从里面挑"，也就不存在分页拼树的问题。

    Raises:
        TraceNotFound: Langfuse 返回 0 条（trace 写错，或已过保留期）
        LangfuseUnavailableError: 读通道不可用（由接口层翻成 degraded）
    """
    rows = await get_langfuse_client().fetch_v2_observations(
        trace_id=trace_id, limit=TRACE_FETCH_LIMIT
    )
    if not rows:
        raise TraceNotFound(f"trace {trace_id} 不存在或已超出保留期")

    spans, total_ms = _build_spans(rows)
    summary = _summarize(trace_id, rows)

    # 详情也带提问原文：从列表点进来后，页面顶部要有一个"这是哪次提问"的锚点。
    # 只有瀑布图的话，切一次窗口/刷新就不知道自己在看哪一轮了。
    # 查不到不报错（历史 / 评测 trace 本来就没有），返回 None 让页面如实显示。
    try:
        question = (await _load_questions([trace_id])).get(trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("详情反查提问失败 trace_id=%s：%s", trace_id, exc)
        question = None

    return {
        "degraded": False,
        "reason": None,
        "trace_id": trace_id,
        "name": summary["name"],
        "kind": summary["kind"],
        "question": question,
        "session_id": summary["session_id"],
        "start_time": summary["start_time"],
        "total_ms": max(total_ms, summary["duration_ms"]),
        "span_count": len(spans),
        "spans": spans,
    }
