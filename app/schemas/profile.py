"""
画像接口的请求/响应模型
======================
三个约定：

1. **显式字段是固定 schema**（`nickname` / `occupation` / `tech_stack` / `answer_style`），
   不是自由 key-value。理由见 `models/user_profile.py` 的 EXPLICIT_FIELDS 注释。

2. `extra="forbid"`：拼错字段名直接 422，而不是静默忽略。
   忽略的后果是"保存成功但没生效"—— 用户反复保存都改不动，接口一直返回 200，
   这类问题几乎不可能从用户反馈里定位。

3. 响应里带 `fields`（字段定义）与 `fact_kinds`，让前端**不用硬编码**中文标签与长度上限。
   多一处硬编码就多一个"后端改了前端没改"的机会。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProfileExplicitPatch(BaseModel):
    """
    PATCH /api/profile 的请求体（四个字段全部可选）。

    "没传"与"传空串"的语义**不同**（接口层负责区分）：
      · 没传   → 保持原值
      · ""     → 删除这个字段
    这正是 PATCH 该有的语义，也是表单里"清空输入框"能生效的前提。
    """

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, description="称呼")
    occupation: str | None = Field(default=None, description="职业 / 身份")
    tech_stack: str | None = Field(default=None, description="技术栈")
    answer_style: str | None = Field(default=None, description="回答风格")


class ProfileFieldOut(BaseModel):
    """一个显式字段的定义（前端据此渲染表单，不硬编码标签）。"""

    key: str
    label: str
    max_length: int
    hint: str


class ProfileFactOut(BaseModel):
    """一条隐式事实。"""

    id: int
    kind: str
    text: str
    confidence: float
    source_session_id: str | None = None
    created_at: str | None = None


class ProfileOut(BaseModel):
    """GET /api/profile 的响应。"""

    explicit: dict[str, str]
    facts: list[ProfileFactOut]
    fields: list[ProfileFieldOut]
    fact_kinds: list[str]


class ProfileUpdateOut(BaseModel):
    """PATCH /api/profile 的响应：返回**保存后**的完整显式字段（前端直接用它刷新表单）。"""

    explicit: dict[str, str]
