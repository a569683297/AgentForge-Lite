"""
审计服务：写一条痕迹 / 读一页痕迹
==================================
两个函数，一条纪律：**写审计永远不能让业务失败**。

为什么单独一个模块而不是把 `AuditLog(...)` 散落到各 service：
  ① 写入需要 `try/except`（见文件头纪律），散落十处就有十次写错的机会；
  ② "用什么字段、action 叫什么"必须一致 —— 查日志的人不该猜"到底是 user.disable
     还是 admin.user_disabled"；
  ③ 将来要换成消息队列 / 外部审计系统时，只改这一个文件。

与 `observability.py`（Langfuse）的分工（容易混）：
  · Langfuse 记的是**一次运行的内部过程**（span 树、耗时、token），面向排障与性能；
  · 审计日志记的是**一次状态变更**（谁、对谁、从什么变成什么），面向合规与追责。
  两者都"记录"，但问题不同。一次对话会产出几十个 span，却通常不产出审计记录。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.audit_log import AuditLog


async def record(
    *,
    action: str,
    actor_id: uuid.UUID | str | None = None,
    actor_email: str = "",
    target_type: str = "",
    target_id: str | uuid.UUID = "",
    detail: dict[str, Any] | None = None,
    ip: str = "",
) -> None:
    """
    写一条审计记录。**永不抛异常**（失败只记 warning）。

    Args:
        action: 动作名，从 `app.models.audit_log` 的 ACTION_* 取，不要就地写字符串
        actor_id: 操作者 id；系统自动动作（如引导管理员创建）为 None
        actor_email: 操作者邮箱快照（账号被删/改邮箱后仍能读懂历史）
        detail: 补充信息，必须是可 JSON 序列化的 dict
    """
    try:
        async with async_session_factory() as session:
            session.add(
                AuditLog(
                    actor_id=uuid.UUID(str(actor_id)) if actor_id else None,
                    actor_email=actor_email or "",
                    action=action,
                    target_type=target_type or "",
                    target_id=str(target_id) if target_id else "",
                    detail=detail,
                    ip=ip or "",
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 见文件头纪律
        # 用 warning 而不是 exception：它不该出现在"服务在报错"的信号里，
        # 但必须留下"这次审计丢了"的痕迹，否则等于静默不记。
        logger.warning("审计写入失败 action=%s target=%s err=%s", action, target_id, exc)


async def list_logs(
    *,
    limit: int = 100,
    offset: int = 0,
    action_prefix: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """
    按时间倒序读审计记录（管理员页面用）。

    Args:
        action_prefix: 按动作前缀过滤，如 `auth.` / `admin.`
        actor_id: 只看某个人做的操作
    """
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if action_prefix:
        # startswith 会生成 LIKE 'auth.%' —— 注意 `%` 由 SQLAlchemy 参数化，不拼字符串
        stmt = stmt.where(AuditLog.action.startswith(action_prefix))
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    stmt = stmt.limit(limit).offset(offset)

    async with async_session_factory() as session:
        rows = (await session.scalars(stmt)).all()

    return [
        {
            "id": row.id,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "actor_email": row.actor_email,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "detail": row.detail,
            "ip": row.ip,
            "created_at": row.created_at,
        }
        for row in rows
    ]
