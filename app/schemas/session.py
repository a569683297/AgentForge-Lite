"""
会话相关的数据结构（D14）
==========================
对应 PRD §11 的两个只读接口：
    GET /api/sessions                 → list[SessionItem]
    GET /api/sessions/{id}/messages   → list[MessageItem]

为什么和 chat.py 里的 ChatRequest/ChatResponse 分开放：
它们是不同的资源 —— 那边是「对话」（一问一答），这边是「会话管理」
（列表 / 历史，将来还会有删除、重命名、导出）。混在一个文件里，
两个方向的改动会互相干扰。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SessionItem(BaseModel):
    """会话列表里的一项。"""

    id: str = Field(description="会话 ID（标准 UUID 字符串，36 位带横杠）")
    title: str = Field(description="会话标题，取自首条用户消息的前 30 字")
    updated_at: datetime = Field(description="最后活跃时间（每轮对话后刷新）")


class MessageItem(BaseModel):
    """会话历史里的一条消息。"""

    role: str = Field(description="消息角色：user / assistant / tool")
    content: str = Field(
        default="",
        description="消息内容；纯工具调用的 assistant 消息内容为空串",
    )
    tool_calls: list | None = Field(
        default=None,
        description="该消息声明的工具调用（原样存的 JSON）；非工具调用消息为 null",
    )
    created_at: datetime = Field(description="落库时间")
