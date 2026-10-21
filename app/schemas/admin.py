"""
管理员接口的数据契约
====================
只放两个东西：改用户状态的请求体、审计记录的输出形状。

为什么 `AdminUserPatch` 的字段全是 `None` 默认值：
  PATCH 的语义是"只改我传了的字段"。用 `exclude_unset` 区分"没传"和"传了 null"，
  否则前端只想改角色时，`is_active` 会被默认值覆盖成 False —— 也就是"顺手把人家禁了"。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AdminUserPatch(BaseModel):
    """改用户状态。两个字段都可选，但**至少要传一个**。"""

    is_active: bool | None = Field(default=None, description="false = 禁用（立即无法登录）")
    role: Literal["user", "admin"] | None = Field(default=None, description="角色")

    @model_validator(mode="after")
    def _at_least_one(self) -> "AdminUserPatch":
        if self.is_active is None and self.role is None:
            raise ValueError("至少要指定 is_active 或 role 之一")
        return self


class AdminUserListOut(BaseModel):
    """
    用户列表。

    为什么带 `total`：前端要显示"共 N 个账号"，而列表是分页的 ——
    让前端拿 `items.length` 当总数，翻到第二页时会显示错。
    """

    total: int
    items: list[dict[str, Any]]


class AuditLogOut(BaseModel):
    """一条审计记录。字段与 `audit_service.list_logs` 的返回一一对应。"""

    id: int
    actor_id: uuid.UUID | None = None
    actor_email: str
    action: str
    target_type: str
    target_id: str
    detail: dict[str, Any] | None = None
    ip: str
    created_at: datetime


class AuditLogListOut(BaseModel):
    total: int
    items: list[AuditLogOut]


class AuditActionOut(BaseModel):
    """可选的动作清单（前端筛选下拉用，避免前端硬编码一份）。"""

    actions: list[str]
