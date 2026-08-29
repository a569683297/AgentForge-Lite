"""
消息模型（messages 表）
=======================
对应 PRD §10 的 messages 表：
id、session_id（外键→sessions.id）、role、content、tool_calls、created_at

关系：一条消息属于一个会话（多对一）
     一个会话有多条消息（一对多）
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Integer

from app.core.db import Base


class Message(Base):
    """会话中的一条消息（user / assistant / tool）。"""

    __tablename__ = "messages"
    # 复合索引：按 (session_id, created_at) 查询会话消息列表时走索引
    __table_args__ = (
        Index("idx_messages_session", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="消息自增ID",
    )
    session_id: Mapped[object] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        comment="所属会话（外键）",
    )
    role: Mapped[str] = mapped_column(
        String(20),
        comment="消息角色：user / assistant / tool",
    )
    content: Mapped[str] = mapped_column(
        String,
        comment="消息内容",
    )
    tool_calls: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
        comment="工具调用记录（JSON 数组），非工具消息为 NULL",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )
