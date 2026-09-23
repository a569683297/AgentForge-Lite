"""
对话记忆服务（两层：Redis 热窗口 + PostgreSQL 兜底）
=====================================================
给 Agent 加上"短期记忆"：记住最近几轮对话，让多轮对话有上下文。

设计要点（为什么这么做）：
1. **滑窗**：只存最近 N 轮，新的进来、旧的挤出去（防止 token 无限膨胀）
2. **Redis**：对话窗口是热数据（高频读写）→ Redis 快；TTL 自动过期，不用手动清理
3. **Upsert 不 Append**：读出整个窗口 → 合并新消息 → 裁剪 → 整体写回
   为什么不能用 rpush/append？
   - 重试/重放会写入重复消息
   - 无法裁剪（append 是单向的，删不掉最旧的）
4. **只存 user 提问 + 最终回答（纯文本）**：不存中间的 tool_calls / tool 消息
   为什么？因为 function calling 协议要求 tool_calls 必须与 tool 消息成对出现；
   如果只存一半（比如存了 tool_calls 却丢了 tool 结果），下一轮请求会 400。
   所以记忆层只保留"对话语义"，工具调用细节留给单轮内处理。
5. **PG 兜底（D15 新增）**：Redis 未命中时从 PostgreSQL 取最近 N 轮并**回填**。
   补的是 PRD F5.3 的后半句 —— 「消息全量 PostgreSQL，热窗口 Redis」。
   D14 之前只有「写 PG」没有「读 PG」，Redis 一过期或一重启，历史就永久消失。

Redis 数据结构（字符串存 JSON 数组）：
  key:   session:{session_id}:window
  value: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
  TTL:   86400 秒（24 小时）
"""

import json
import uuid

from app.core.logging import logger
from app.core.redis import redis_client

# ---- 滑窗配置 ----
WINDOW_TURNS = 8                          # 保留最近 8 轮对话
MAX_MESSAGES = WINDOW_TURNS * 2           # 每轮 = user + assistant = 2 条消息
WINDOW_TTL = 86400                        # 24 小时过期（D4 学的 TTL）


def _window_key(session_id: uuid.UUID | str) -> str:
    """
    生成 Redis key。
    命名规范：冒号分层（session:{id}:window）—— 便于按前缀批量查/管理，
    这也是 Redis 社区的通用约定（类比文件路径分层）。

    ⚠ 这里的 f-string 会把 uuid.UUID 渲染成**带横杠**的规范形态。
    所以必须保证调用方传进来的 id 已经是规范形态 —— 这正是 D14 把
    chat.py 的 `uuid4().hex`（32 位无横杠）收口成 `uuid4()` 的原因：
    PG 会把无横杠写法规范化后存库，从库里读出来的 id 拼出的 key
    与当初写进去的那个不同一个，热窗口就会静默读不到。
    """
    return f"session:{session_id}:window"


def _to_uuid(session_id: uuid.UUID | str) -> uuid.UUID | None:
    """
    把 id 归一成 uuid.UUID；不是 uuid 形态则返回 None。

    为什么需要它：sessions.id 是 PG 的 uuid 类型，兜底查询必须传 uuid；
    而本模块的历史调用方（如 d8 的"纯 Redis"用例）用的是普通字符串 id。
    对这类 id，PG 里不可能存在对应会话，兜底无从谈起 → 返回 None 让它跳过。
    """
    if isinstance(session_id, uuid.UUID):
        return session_id
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, AttributeError, TypeError):
        return None


async def get_window(session_id: uuid.UUID | str) -> list[dict]:
    """
    读取该会话的滑窗历史消息（两级读取：Redis 优先，PG 兜底）。

    读取顺序：
        ① Redis 命中 → 直接返回（快路径，PG 全程不被碰）
        ② Redis 未命中 / 数据损坏 → 从 PG 取最近 N 轮并**回填 Redis**（D15）

    ⚠ 第 ② 步为什么是「读 + 写回」而不是「只读出来返回」：
    append_turn 内部复用的就是本函数（见 append_turn 第 ① 步），所以**只读不写回不会丢历史** ——
    实测证据见 `scripts/d15_backfill_verify.py` 的 B 段对照实验：
    在没有回填的情况下，append_turn 照样能读到 16 条完整历史并原样写回。
    回填的真正收益是两条：
      ① **避免同一轮查两次 PG**：本函数一次 + append_turn 内部一次（实测该路径下确实是两次）
      ② **让热窗口尽早收敛回权威源**：本轮即使中途失败（LLM 超时/异常），
         下一次请求也能直接命中，而不必再兜底查一次库
    这也是缓存 read-repair 的标准做法：缓存被读过之后，就应该与源头保持一致。

    Returns:
        消息列表（时间正序）；无历史时返回空列表（不抛异常，保证对话可用）
    """
    raw = await redis_client.get(_window_key(session_id))
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 数据损坏 → 这份缓存不可信：往下走兜底重建，而不是当作"无历史"返回空
            logger.warning("滑窗数据解析失败，将从 PG 重建 session=%s", session_id)

    return await _rebuild_from_pg(session_id)


async def _rebuild_from_pg(session_id: uuid.UUID | str) -> list[dict]:
    """
    PG 兜底：从 messages 表取最近 N 轮「对话语义」，并回填 Redis 热窗口（D15 / PRD F5.3）。

    两个设计选择（都别省）：
      ① **非 uuid 的 id 直接跳过**：sessions.id 是 uuid 类型，非 uuid 形态的 id
         在 PG 里不可能存在，查是白查。顺带也保住了 d8 那个"纯 Redis 用例"的语义。
      ② **只回填非空结果**：PG 也没有历史时（全新会话），不往 Redis 写空数组 ——
         避免把「PG 此刻也没有」这个瞬时结论固化成窗口内容。
    """
    sid = _to_uuid(session_id)
    if sid is None:
        return []

    # 延迟导入：记忆层（热）依赖持久层（冷）这条方向要保持单向。
    # 写成函数内导入，将来 session_service 若反过来需要记忆层也不会变成循环导入。
    from app.services.session_service import get_recent_dialogue

    history = await get_recent_dialogue(sid, turns=WINDOW_TURNS)
    if not history:
        return []

    await redis_client.set(
        _window_key(session_id),
        json.dumps(history, ensure_ascii=False),
        ex=WINDOW_TTL,
    )
    logger.info(
        "热窗口未命中，已从 PG 回填 session=%s 条数=%d", session_id, len(history)
    )
    return history


async def append_turn(
    session_id: uuid.UUID | str, user_content: str, assistant_content: str
) -> None:
    """
    写入一轮对话（Upsert：读 → 合并 → 裁剪 → 整体写回）。

    Args:
        session_id: 会话 ID
        user_content: 用户这轮说的话
        assistant_content: Agent 这轮的回答
    """
    # ① 读出当前窗口。
    #    ⚠ 这里复用 get_window（而不是直接读 Redis）：所以它自带 PG 兜底 ——
    #    Redis 是空的也不会把本轮写成"只剩 1 轮"的残缺窗口。
    #    换句话说，「回填」不是这个函数的正确性前提（见 get_window 的说明）。
    window = await get_window(session_id)
    window.append({"role": "user", "content": user_content})        # ② 追加本轮
    window.append({"role": "assistant", "content": assistant_content})
    window = window[-MAX_MESSAGES:]                # ③ 裁剪：只留最近 N 条（滑窗）

    # ④ 整体写回（ensure_ascii=False 让中文可读，不转成 \uXXXX）
    await redis_client.set(
        _window_key(session_id),
        json.dumps(window, ensure_ascii=False),
        ex=WINDOW_TTL,                             # TTL：24 小时后自动过期
    )
    logger.info(
        "记忆已更新 session=%s 窗口长度=%d/%d",
        session_id,
        len(window),
        MAX_MESSAGES,
    )


async def clear_window(session_id: uuid.UUID | str) -> None:
    """清空某会话的记忆（用户点"新对话"时用）。"""
    await redis_client.delete(_window_key(session_id))
    logger.info("记忆已清空 session=%s", session_id)
