"""
对话记忆服务（滑窗 + Redis）
=============================
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

Redis 数据结构（字符串存 JSON 数组）：
  key:   session:{session_id}:window
  value: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
  TTL:   86400 秒（24 小时）
"""

import json

from app.core.logging import logger
from app.core.redis import redis_client

# ---- 滑窗配置 ----
WINDOW_TURNS = 8                          # 保留最近 8 轮对话
MAX_MESSAGES = WINDOW_TURNS * 2           # 每轮 = user + assistant = 2 条消息
WINDOW_TTL = 86400                        # 24 小时过期（D4 学的 TTL）


def _window_key(session_id: str) -> str:
    """
    生成 Redis key。
    命名规范：冒号分层（session:{id}:window）—— 便于按前缀批量查/管理，
    这也是 Redis 社区的通用约定（类比文件路径分层）。
    """
    return f"session:{session_id}:window"


async def get_window(session_id: str) -> list[dict]:
    """
    读取该会话的滑窗历史消息。

    Returns:
        消息列表；无历史或数据损坏时返回空列表（不抛异常，保证对话可用）
    """
    raw = await redis_client.get(_window_key(session_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 数据损坏（理论上不该发生）→ 当作无历史，并告警
        logger.warning("滑窗数据解析失败，已忽略 session=%s", session_id)
        return []


async def append_turn(session_id: str, user_content: str, assistant_content: str) -> None:
    """
    写入一轮对话（Upsert：读 → 合并 → 裁剪 → 整体写回）。

    Args:
        session_id: 会话 ID
        user_content: 用户这轮说的话
        assistant_content: Agent 这轮的回答
    """
    window = await get_window(session_id)          # ① 读出当前窗口
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


async def clear_window(session_id: str) -> None:
    """清空某会话的记忆（用户点"新对话"时用）。"""
    await redis_client.delete(_window_key(session_id))
    logger.info("记忆已清空 session=%s", session_id)
