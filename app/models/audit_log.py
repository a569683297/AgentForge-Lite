"""
审计日志（audit_logs 表）
==========================
一行 = 一件"值得留下痕迹"的事：登录、改密、账号被禁用、角色被提升、文档被删……

为什么需要它（不是"为了完整而加一张表"）：
  账号体系一旦上线，"谁在什么时候做了什么"就变成了必须能回答的问题。
  没有审计日志时，这些答案只存在于**应用日志**里 —— 而应用日志会轮转、会被清理，
  也没有结构化字段可供查询（想查"上周被禁用的账号有哪些"，只能 grep 文本）。
  更关键的是：**管理员自己也需要被审计**。禁用一个账号、把某人提成 admin，
  这些动作如果没有留痕，出了事就只剩"他说他没做"。

设计上的四个取舍：

1. **只记录"改变状态"与"认证边界"的事件，不记录读操作**
   记录每次 GET 会让表以访问量级增长（读远多于写），而排障时真正要找的
   几乎都是写操作。这不是遗漏，是刻意的范围：审计日志不是访问日志（access log）。

2. **`actor_email` 冗余存一份快照**
   正常做法是只存 actor_id 再 JOIN users。这里两份都存，因为：
   账号可能被删、邮箱可能被改 —— 而"当时是谁干的"必须保持当时的模样。
   审计表的语义是**事实记录**，不是"当前状态的视图"。

3. **`detail` 用 JSONB 而不是拼接字符串**
   "把 {role: user → admin} 记成一句话"之后就再也查不了了。
   JSONB 既能给人看，也能 `WHERE detail->>'from' = 'user'` 地查。

4. **写入失败不能拖垮业务**
   `record()` 内部吞掉所有异常，只记一条 warning。
   反过来的话，审计表写不进去就会让"登录"失败 —— 那是本末倒置：
   审计是旁证，不是前置条件。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# ---- 动作名（唯一出处）----
# 用 "领域.动作" 的命名而不是零散字符串：查询时能按前缀分组（auth.* 全是认证类）。
ACTION_LOGIN = "auth.login"
ACTION_LOGIN_FAILED = "auth.login_failed"
ACTION_LOGOUT = "auth.logout"
ACTION_REGISTER = "auth.register"
ACTION_PASSWORD_CHANGED = "auth.password_changed"
ACTION_PASSWORD_RESET = "auth.password_reset"
ACTION_USER_DISABLED = "admin.user_disabled"
ACTION_USER_ENABLED = "admin.user_enabled"
ACTION_USER_ROLE_CHANGED = "admin.user_role_changed"
ACTION_DOCUMENT_DELETED = "document.deleted"
ACTION_TOOL_SERVER_UPLOADED = "tool_server.uploaded"
ACTION_TOOL_SERVER_DELETED = "tool_server.deleted"

ACTIONS: tuple[str, ...] = (
    ACTION_LOGIN,
    ACTION_LOGIN_FAILED,
    ACTION_LOGOUT,
    ACTION_REGISTER,
    ACTION_PASSWORD_CHANGED,
    ACTION_PASSWORD_RESET,
    ACTION_USER_DISABLED,
    ACTION_USER_ENABLED,
    ACTION_USER_ROLE_CHANGED,
    ACTION_DOCUMENT_DELETED,
    ACTION_TOOL_SERVER_UPLOADED,
    ACTION_TOOL_SERVER_DELETED,
)


class AuditLog(Base):
    """一条审计记录。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        # 列表页的主查询是"按时间倒序 + 可按 actor 过滤"，这条复合索引直接服务它
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL 而不是 CASCADE：账号被删掉之后，它做过的**事**仍然要留在账上。
        # 级联删除会让"删号"变成"抹掉痕迹"——那就正好成了滥用的出口。
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="操作者 id（账号被删后置空，但 actor_email 仍保留当时的邮箱）",
    )
    actor_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="操作者邮箱**快照**（见文件头第 2 条：账号改了邮箱也不能改写历史）",
    )
    action: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="动作名，取值见 ACTION_* / ACTIONS",
    )
    target_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="被操作对象的类型：user / document / tool_server / session …（空 = 无具体对象）",
    )
    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="被操作对象 id（统一存字符串：可能是 uuid、字符串键或自增 id）",
    )
    detail: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="补充信息（JSONB）：如 {from: 'user', to: 'admin'}",
    )
    ip: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        server_default=text("''"),
        comment="来源 IP。⚠ 反向代理后取到的是代理地址，需要信任 X-Forwarded-For 才准（本项目未做）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} by={self.actor_email} target={self.target_type}:{self.target_id}>"
