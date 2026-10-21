"""
用户模型（users 表）
====================
账号体系的地基。设计上只做三件事：**认人、分权、可禁用**。

为什么 `email` 存小写：
  唯一约束是**字节级**的 —— `A@x.com` 与 `a@x.com` 在 PostgreSQL 眼里是两个人。
  邮箱的域名部分大小写不敏感、本地部分在实践中也不敏感（虽然 RFC 5321 说敏感），
  所以统一转小写再存，并在此基础上建唯一索引。转换动作放在 service 层
  （`normalize_email`），不在数据库做 —— 否则"库里存的是小写"这件事就没人能保证。

为什么 `role` 用字符串而不是枚举类型：
  PostgreSQL 的 ENUM 改值要 ALTER TYPE，而角色是最可能增加的东西（user/admin/support）。
  字符串 + 应用层白名单（`ROLES`）够用，且改起来只是改一个常量。

为什么不用 `is_active` 而是 `disabled_at`（这里用 is_active）：
  两种都在用。选布尔是为了查询直观（`WHERE is_active`），代价是丢失"什么时候被禁的" ——
  那件事交给审计日志，不该塞进用户表。

⚠ 密码永远不出现在这个模型之外：`password_hash` 只由 `auth_service` 读写，
  任何接口都不得把它序列化出去（schemas 里也没有这个字段，双保险）。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# 角色白名单（唯一出处：接口层的 Literal、迁移校验、前端下拉都从这里取）
ROLES: tuple[str, ...] = ("user", "admin")

ROLE_USER = "user"
ROLE_ADMIN = "admin"


class User(Base):
    """一个账号。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="用户 id",
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
        comment="登录名（**统一小写存储**，见文件头）",
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="bcrypt 哈希串（$2b$12$...），明文永不落库",
    )
    display_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="显示名（可空串；列表里回退到邮箱前缀）",
    )
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ROLE_USER,
        comment="user | admin（取值见 ROLES）",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="false = 已禁用，登录与被鉴权一律拒绝",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最后一次成功登录的时刻（审计与「僵尸账号」清理的依据）",
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "最后一次改密时刻。"
            "⚠ 它**不能**吊销已签发的 access token（JWT 无状态），"
            "但可以让 refresh 流程拒绝「改密前签发的」凭证 —— 见 auth_service.refresh"
        ),
    )

    def __repr__(self) -> str:
        # 绝不打印 password_hash（模型被塞进日志的场合比想象中多）
        return f"<User {self.email} role={self.role} active={self.is_active}>"
