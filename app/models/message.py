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
    trace_id: Mapped[str | None] = mapped_column(
        # 32 = Langfuse/OTEL 的 trace id 宽度（十六进制，实测 738cbe936fb6de9afa6404654f251289）。
        #
        # 为什么业务表要存一个观测系统的 id —— 因为这是「Trace 页按提问展示」的唯一凭证：
        #   · Langfuse v2 observations **不返回 input**（实测键集里根本没有这个字段），
        #     所以"这次提问问了什么"只能从自己库里取；
        #   · v1 的 /api/public/traces 带 input，但 2026-11-16 下线，且一次只能查一条。
        # 把 id 落在 user 消息上，Trace 页一次 SQL 就能批量补出提问原文，零额外配额。
        #
        # ⚠ **只有 role=user 的行有值**：一次提问 = 一条 trace = 一条 user 消息。
        #   assistant/tool 是同一轮的后续消息，重复挂同一个 id 会让 JOIN 出多行，
        #   把"一次提问"变成"三条记录"，去重逻辑又要多一层。
        String(32),
        nullable=True,
        index=True,
        comment="产生这条消息的那次运行的 Langfuse trace id（仅 role=user 有）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )
