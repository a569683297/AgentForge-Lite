"""
消息模型（messages 表）
=======================
对应 PRD §10 的 messages 表：
id、session_id（外键→sessions.id）、role、content、tool_calls、created_at

关系：一条消息属于一个会话（多对一）
     一个会话有多条消息（一对多）
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB, UUID

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
        # ⚠ 必须是 JSONB + none_as_null=True，两个都不能省：
        #   ① JSONB 对齐 PRD §10 的定义（库里原先建成 json，类型不一致）；
        #   ② SQLAlchemy 的 JSON/JSONB 默认 none_as_null=False —— Python 的 None 会被
        #      序列化成 **JSON 字面量 null** 存进去，而不是 SQL NULL。实测后果：
        #      `WHERE tool_calls IS NULL` 永远查不到行，等于把"这条消息到底有没有工具调用"
        #      的判断整个毁掉（D15 的 PG 回填正是靠它筛消息）。
        JSONB(none_as_null=True),
        nullable=True,
        comment="工具调用记录（JSONB 数组），非工具消息为 SQL NULL",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )
