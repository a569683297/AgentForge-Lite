"""
画像抽取器（把对话里的稳定信息抽成事实）
========================================
链路：一次对话结束 → 触发检查 → 后台任务 → LLM 抽取 → 去重入库 → 下次对话生效。

为什么**不能**挂在对话请求路径上
--------------------------------
一轮对话已经串了：读记忆 → LLM 多轮工具调用 → 摘要检查。
再同步加一次 LLM 抽取，每轮多 1~2 秒；而**绝大多数轮次根本不含画像信息**
（"我叫老郭"这种话一生可能只说一次）。

所以：**触发条件 + 后台执行**。
  · 触发：该用户新增 ≥6 条发言，且距上次抽取 ≥10 分钟
  · 执行：`asyncio.create_task`（与 memory_service 里"将来若要异步化"的注释同一条思路）
  · 失败：只记日志，下一轮再试 —— **绝不因为抽画像把对话搞挂**

与摘要（`maybe_summarize`）的对照，这个差异值得记住：
| | 摘要 | 画像抽取 |
|---|---|---|
| 谁等它 | 下一轮对话要读它 | 没人等 |
| 延迟敏感 | 是（同步 await） | 否（后台任务） |
| 失败后果 | 记忆断档 | 个性化晚一点生效 |
判据不是"哪个更高级"，而是**这个结果会不会被下一步立刻读到**。

游标与冷却为什么放 Redis
------------------------
它们都是**可丢失的状态**：
  · 游标丢了 → 下次抽最近的 30 条（不是全量重抽，见 collect_new_user_messages）
  · 冷却时间丢了 → 最多多抽一次，去重约束保证不会重复入库
这是"能不能放缓存"的标准判断：先问丢了会怎样。答"只会多做一次无害的重复劳动"就能放。
（同样的判断在 memory_service 的摘要游标上已经用过一次。）

⚠ 与认证的 Redis 白名单**刚好相反**：那边丢了就拒绝（fail-closed），这边丢了照常跑（fail-open）。
  判据是"这层是不是业务的前置条件"——见 profile_service 文件头的表格。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.core.redis import redis_client
from app.models.message import Message
from app.models.session import Session as ChatSession
from app.services import profile_service

# ---- 触发参数（调优入口，与 memory_service 的 SUMMARY_* 同族）----
PROFILE_TRIGGER_MESSAGES = 6      # 新增这么多条用户发言才抽一次
PROFILE_COOLDOWN_SECONDS = 600    # 两次抽取至少间隔 10 分钟
PROFILE_BATCH_MAX = 30            # 单次最多读多少条用户发言（防止一次请求无界工作）
PROFILE_EXISTING_MAX = 30         # 喂给模型的"已有画像"最多几条（防止 prompt 无限长）
PROFILE_TEMPERATURE = 0.1         # 抽取要"忠实转录"，不要创作

_CURSOR_TTL = 30 * 86400          # 游标 30 天：比冷却期长得多，过期只会导致"多抽一次最近的"
_LAST_AT_TTL = 7 * 86400


# ============================================================
# Redis 容错层（失败不抛）
# ============================================================
async def _redis_get(key: str) -> str | None:
    try:
        return await redis_client.get(key)
    except Exception as e:  # noqa: BLE001
        logger.warning("画像状态读取失败（按无状态处理）key=%s err=%s", key, e)
        return None


async def _redis_set(key: str, value: str, *, ex: int) -> None:
    try:
        await redis_client.set(key, value, ex=ex)
    except Exception as e:  # noqa: BLE001
        logger.warning("画像状态写入失败（下轮重试）key=%s err=%s", key, e)


def _cursor_key(owner_id: uuid.UUID | str) -> str:
    return f"profile:{owner_id}:extract_upto"


def _last_at_key(owner_id: uuid.UUID | str) -> str:
    return f"profile:{owner_id}:extract_at"


async def _get_cursor(owner_id: uuid.UUID | str) -> int | None:
    raw = await _redis_get(_cursor_key(owner_id))
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("画像游标值异常，按无游标处理 owner=%s", owner_id)
        return None


async def _set_cursor(owner_id: uuid.UUID | str, message_id: int) -> None:
    await _redis_set(_cursor_key(owner_id), str(message_id), ex=_CURSOR_TTL)


async def _get_last_at(owner_id: uuid.UUID | str) -> float | None:
    raw = await _redis_get(_last_at_key(owner_id))
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _set_last_at(owner_id: uuid.UUID | str, ts: float) -> None:
    await _redis_set(_last_at_key(owner_id), f"{ts:.0f}", ex=_LAST_AT_TTL)


# ============================================================
# 读消息
# ============================================================
async def collect_new_user_messages(
    owner_id: uuid.UUID, *, after_id: int | None = None, limit: int = PROFILE_BATCH_MAX
) -> list[dict]:
    """
    取该用户「游标之后」的发言（**只取 role='user'**，理由见 prompts/profiler.py）。

    ⚠ `after_id=None`（游标丢了）时的行为是**取最近 limit 条**，不是"从头取全部"：
      从头取意味着老用户第一次跑抽取会把几百条历史一次性喂给 LLM ——
      token 爆炸、延迟几十秒，而且抽出来的都是早就过时的东西。
      最近 30 条足够覆盖"他最近在干什么"，这才是画像要的时效性。

    跨会话：JOIN sessions 按 owner 过滤，所以同一个用户在不同会话里说的话
    会被合并成一条流。这正是"跨会话画像"的实现要点 —— 画像不认 session。
    """
    async with async_session_factory() as db:
        stmt = (
            select(Message.id, Message.content, Message.session_id)
            .join(ChatSession, ChatSession.id == Message.session_id)
            .where(ChatSession.owner_id == owner_id, Message.role == "user")
        )
        if after_id is not None:
            rows = (
                await db.execute(
                    stmt.where(Message.id > after_id).order_by(Message.id).limit(limit)
                )
            ).all()
        else:
            # 游标缺失：倒序取最近 N 条，再反转成时间正序
            rows = list(
                reversed(
                    (
                        await db.execute(stmt.order_by(Message.id.desc()).limit(limit))
                    ).all()
                )
            )
    # content 可能是空串（极少见：空消息被落库）→ 丢掉，避免喂一堆空行给 LLM
    return [
        {"id": r.id, "content": (r.content or "").strip(), "session_id": r.session_id}
        for r in rows
        if (r.content or "").strip()
    ]


# ============================================================
# 解析模型输出
# ============================================================
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def parse_facts_json(raw: str) -> list[dict]:
    """
    把模型输出解析成事实列表。**解析失败抛 ValueError**（由调用方决定怎么处置）。

    要处理三种现实中的脏输出（都实测见过）：
      ① ```json ... ``` 代码块包裹（尽管 prompt 明说了不要）
      ② 前后带一句解释（"好的，以下是提取结果："）
      ③ 单个对象而不是数组（模型偶尔只抽到一条就忘了包数组）

    第 ③ 种尤其值得处理：直接 `isinstance(data, list)` 判死会让"只有一条事实"
    这个最常见的场景每次都失败 —— 而失败是静默的（下次重试还是同一条）。
    """
    s = _FENCE.sub("", (raw or "").strip())
    start, end = s.find("["), s.rfind("]")
    if start != -1 and end > start:
        s = s[start : end + 1]
    else:
        # 没有数组 → 尝试当成单个对象
        obj_start, obj_end = s.find("{"), s.rfind("}")
        if obj_start == -1 or obj_end <= obj_start:
            raise ValueError("输出里既没有 JSON 数组也没有 JSON 对象")
        s = f"[{s[obj_start : obj_end + 1]}]"

    data = json.loads(s)
    if not isinstance(data, list):
        raise ValueError(f"期望数组，实际 {type(data).__name__}")

    facts = [
        item
        for item in data
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    return facts


# ============================================================
# 抽取
# ============================================================
def _format_dialogue(rows: list[dict]) -> str:
    """一行一条、带序号 —— 序号让模型能引用"第 3 条说的"，也让日志好对照。"""
    return "\n".join(f"{i}. {r['content']}" for i, r in enumerate(rows, start=1))


async def _extract(
    owner_id: uuid.UUID, session_id: uuid.UUID | None, rows: list[dict]
) -> dict:
    """真正做抽取。返回 {"extracted": bool, "reason": str, "added": int}。"""
    from app.prompts.profiler import EXTRACT_SYSTEM_PROMPT, build_extract_user_prompt
    from app.services.llm_gateway import chat

    existing = [f.text for f in await profile_service.list_facts(owner_id, limit=PROFILE_EXISTING_MAX)]
    try:
        raw = await chat(
            [
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_extract_user_prompt(_format_dialogue(rows), existing),
                },
            ],
            trace_name="profile-extract",
            temperature=PROFILE_TEMPERATURE,
        )
    except Exception:
        # LLM 挂了：不推进游标，但**写冷却时间** —— 否则下一轮对话会立刻再试一次，
        # 于是一次 LLM 故障会变成"每轮都多一次失败调用"，把故障放大成雪崩。
        logger.exception("画像抽取 LLM 调用失败（不影响对话）owner=%s", owner_id)
        await _set_last_at(owner_id, time.time())
        return {"extracted": False, "reason": "llm_failed", "added": 0}

    try:
        facts = parse_facts_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("画像抽取输出无法解析（不影响对话）owner=%s err=%s", owner_id, e)
        await _set_last_at(owner_id, time.time())
        return {"extracted": False, "reason": "bad_json", "added": 0}

    added = await profile_service.add_facts(owner_id, facts, source_session_id=session_id)
    await _set_cursor(owner_id, rows[-1]["id"])
    await _set_last_at(owner_id, time.time())
    return {"extracted": True, "reason": "ok", "added": added, "candidates": len(facts)}


async def maybe_extract(owner_id: uuid.UUID | None, session_id: uuid.UUID | None) -> dict:
    """
    检查是否该抽一次；该抽就抽。返回结构化结果（便于脚本与测试断言）。

    reason 取值：no_owner / below_trigger / cooldown / llm_failed / bad_json / ok

    判断顺序即优先级：
      ① 没有归属（系统调用/评测）→ 直接跳过
      ② 新增发言不足 6 条 → 攒着
      ③ 距上次不足 10 分钟 → 等
      ④ 抽取
    """
    if owner_id is None:
        return {"extracted": False, "reason": "no_owner", "added": 0}

    cursor = await _get_cursor(owner_id)
    rows = await collect_new_user_messages(owner_id, after_id=cursor)
    if len(rows) < PROFILE_TRIGGER_MESSAGES:
        return {
            "extracted": False,
            "reason": "below_trigger",
            "added": 0,
            "pending": len(rows),
        }

    last_at = await _get_last_at(owner_id)
    if last_at is not None and time.time() - last_at < PROFILE_COOLDOWN_SECONDS:
        return {"extracted": False, "reason": "cooldown", "added": 0, "pending": len(rows)}

    return await _extract(owner_id, session_id, rows)


# ============================================================
# 后台调度
# ============================================================
# ⚠ 必须持有强引用：`asyncio.create_task` 返回的 task 若没人引用，
#   可能被 GC 在跑完之前回收（Python 文档明确要求保存引用）。
#   这个坑的症状极隐蔽 —— 日志里没有错误、没有 traceback，只是"有时抽有时不抽"。
_BACKGROUND: set[asyncio.Task] = set()


async def _safe_extract(owner_id: uuid.UUID, session_id: uuid.UUID | None) -> None:
    """后台任务的入口：**任何异常都必须在这里被吞掉**。

    不吞的话，异常会在事件循环层被记成 "Task exception was never retrieved"，
    而那个日志出现在请求结束之后，跟用户看到的任何现象都对不上 —— 排障时几乎无法归因。
    """
    try:
        result = await maybe_extract(owner_id, session_id)
        logger.info("画像抽取完成 owner=%s 结果=%s", owner_id, result)
    except Exception:
        logger.exception("画像抽取后台任务异常（对话不受影响）owner=%s", owner_id)


def schedule_extract(owner_id: uuid.UUID | None, session_id: uuid.UUID | None) -> bool:
    """
    把抽取丢到后台。**同步函数、永不抛**（调用点在对话收尾，不能让它影响响应）。

    Returns:
        True = 任务已提交；False = 没有事件循环（脚本/测试环境）或没有归属
    """
    if owner_id is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环（同步脚本里调用）→ 不抽，也不报错。
        logger.debug("无事件循环，跳过画像后台抽取 owner=%s", owner_id)
        return False

    task = loop.create_task(_safe_extract(owner_id, session_id))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return True
