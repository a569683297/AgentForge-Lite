"""
会话模型（sessions 表）
=======================
对应 PRD §10 的 sessions 表：
id（UUID 主键）、title、summary（长期摘要）、created_at、updated_at

说明：Python 3.12 写法 Mapped[...] + mapped_column() 是 SQLAlchemy 2.0 新风格。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _uuid() -> uuid.UUID:
    """生成 UUID 主键（用函数而非直接调用，保证每条记录生成新值）。"""
    return uuid.uuid4()


class Session(Base):
    """一次对话会话。"""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid,
        comment="会话唯一标识",
    )
    title: Mapped[str] = mapped_column(
        String(255),
        default="新会话",
        comment="会话标题（可后续用首条消息自动生成）",
    )
    summary: Mapped[str | None] = mapped_column(
        String,  # text
        nullable=True,
        comment="长期记忆摘要（超20轮对话由LLM生成）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),   # 数据库侧默认值（不用应用传）
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),         # 更新时自动刷新（ORM 层生效）
        comment="最后更新时间",
    )
