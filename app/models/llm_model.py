"""
模型注册表 ORM（llm_models 表）
================================
只存"用户自己添加的"自定义模型；内置模型（deepseek / openai 兼容）由 .env 驱动、
**不入库** —— 见《模型切换与自定义模型-需求稿-2026-10-13》§5.1。
职责分工：`.env` 是"我信任的默认通道"，本表是"用户自己加的"。

为什么自定义模型**必须**入库（而不是像 .env 那样静态配置）：
    LLM 调用发生在**后端**（架构红线：业务代码不得直连 SDK，统一走网关）。
    前端 localStorage 里的 key，后端的网关**用不到** ——
    要让"用户添加的模型"真的能用来发问，三件套（base_url / api_key / model）
    必须存在后端能读到的地方。本项目就是 PostgreSQL 这张表。

对外主键的用法：本表的 `id` 就是对话请求里 `model_id` 的取值之一
（另一个取值是内置通道名，如 "deepseek"——见 model_service._resolve 系列，S2 接）。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _uuid() -> uuid.UUID:
    """生成 UUID 主键（用函数而非直接调用，保证每条记录生成新值）。"""
    return uuid.uuid4()


class LlmModel(Base):
    """一条自定义模型配置。"""

    __tablename__ = "llm_models"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid,
        comment="模型 ID（对话请求 model_id 的取值之一）",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        comment="显示名（用户可读，如「我的 Qwen」）",
    )
    base_url: Mapped[str] = mapped_column(
        String(500),
        comment="OpenAI 兼容端点根地址（已规范化：去尾斜杠），如 https://api.example.com/v1",
    )
    api_key: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="API Key（本地端点可空）。⚠ 明文存储（需求稿 §4.4 已拍板）：出口打码、不进日志",
    )
    model: Mapped[str] = mapped_column(
        String(255),
        comment="模型标识（发给端点的 model 字段），如 qwen-max",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),   # 库级默认也写上：直接 SQL 插入时不用显式给值
        comment="是否启用（前端置灰而不删，避免误删后还要重新填 key）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="最后更新时间（ORM 层更新时自动刷新）",
    )

    def __repr__(self) -> str:
        """
        repr **一律打码 api_key**。

        为什么必须覆盖：SQLAlchemy 默认 repr 会打印**所有列的实际值** ——
        于是任何一句 `logger.info("model=%s", row)`、任何一次把对象塞进异常消息、
        乃至调试时在终端敲一下变量名，都会把 key 明文写出去，看起来却"只是打了一行日志"。
        这与 `Settings.__repr_args__`（app/config.py）是同一手法：
        在**出口处主动屏蔽**，而不是指望每个人都记得别打。
        """
        return (
            f"<LlmModel id={str(self.id)[:8]}... name={self.name!r} "
            f"model={self.model!r} base_url={self.base_url!r} "
            f"api_key={'***' if self.api_key else None}>"
        )
