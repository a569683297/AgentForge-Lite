"""
工具服务器 ORM（tool_servers 表）
==================================
一张表回答一个问题：**谁上传过哪些工具程序，它们现在该不该跑**。

为什么必须入库（而不是像 D27 的 inventory / harness 那样写在 .env 里）：
    .env 是"部署时定死的配置"，改它需要重启 + 有权限。
    而"用户上传"意味着**运行期不断新增**的东西 —— 它的生命周期、状态、
    失败原因都必须能被查询、被重启后恢复。这些能力只有库能给。

    ⚠ 重启恢复是这张表存在的**首要理由**：容器是 --rm 的一次性进程，
    api 一重启它们就全没了。没有这张表，用户上传的工具"重启即消失"，
    而界面上看不出任何异常（列表是空的，像从没传过）。

与注册表的分工（别混）：
    · 本表      = 持久事实（代码在哪、名字叫啥、该不该跑）
    · ToolRegistry（内存）= 当前事实（此刻真的有哪些工具可调）
    两者可以不一致 —— 而那种不一致恰恰是排障要看的东西（同 /api/tools 的设计）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# ---- 状态取值（唯一合法清单）----
# 与 RETRIEVER_CONFIGS / LLM_CHANNELS 同一个手法：合法值只有一个出处，
# 校验器、接口层、迁移脚本都从这里取，不各写一份字符串字面量。
STATUS_ACTIVE = "active"      # 该跑，且启动时会自动拉起
STATUS_DISABLED = "disabled"  # 用户主动停用：不拉起、工具不进注册表
STATUS_FAILED = "failed"      # 拉起失败（代码有错/镜像缺失）。**保留**，等重连
TOOL_SERVER_STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_FAILED)


def _uuid() -> uuid.UUID:
    """生成 UUID 主键（用函数而非直接调用，保证每条记录生成新值）。"""
    return uuid.uuid4()


class ToolServer(Base):
    """一台「用户上传的 MCP Server」。"""

    __tablename__ = "tool_servers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid,
        comment="主键。同时是磁盘上的子目录名（data/tool_servers/<id>/）",
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment=(
            "上传者（审计用）。⚠ 上传工具是**管理员能力**，不是每个用户的能力："
            "工具会进进程级 ToolRegistry，而 LLM 看到的是全局工具表 —— "
            "两个用户的同名工具会在注册表里撞名。要真正按人隔离工具，"
            "得给每个请求一份『可见工具视图』，那是另一件事（见 auth 知识点文档）。"
        ),
    )
    name: Mapped[str] = mapped_column(
        String(32),
        unique=True,          # ★ 库级唯一：应用层校验会被并发绕过，约束不会
        index=True,
        comment="机器名。同时是工具名前缀（命名空间）与注册表的 source 值",
    )
    display_name: Mapped[str] = mapped_column(
        String(100),
        comment="显示名（用户可读，如「我的报销工具」）",
    )
    description: Mapped[str] = mapped_column(
        Text,
        default="",
        server_default=text("''"),
        comment="给人看的说明（不发给 LLM —— 工具描述在代码里，由 tools/list 现取）",
    )
    entry_file: Mapped[str] = mapped_column(
        String(255),
        comment="相对工具根目录的入口路径，形如 <uuid>/server.py。不存绝对路径 —— 换机器/换挂载点就失效",
    )
    sha256: Mapped[str] = mapped_column(
        String(64),
        comment="代码指纹。用来回答「容器里跑的是不是我刚传的那份」—— 同名覆盖时这是唯一线索",
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer,
        comment="代码字节数（上传上限由 service 层拦，这里只记录事实）",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=STATUS_ACTIVE,
        server_default=text("'active'"),
        comment="active | disabled | failed —— 取值见 TOOL_SERVER_STATUSES",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="最近一次拉起失败的原因（成功时清空）。失败原因必须能看见，否则用户只能对着空列表猜",
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
        """repr 只带标识与状态 —— 不含代码内容，避免日志里出现整段源码。"""
        return (
            f"<ToolServer id={str(self.id)[:8]}... name={self.name!r} "
            f"status={self.status!r} size={self.size_bytes}B>"
        )
