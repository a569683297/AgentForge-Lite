"""
工具服务器接口的数据契约
=========================
只描述"跨网络的形状"。校验规则在 service 层（那边能被单测直接打），
这里不做二次校验 —— 同一个规则写两遍，早晚会两处不一致。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ToolServerOut(BaseModel):
    """
    一台用户上传的工具 server。

    字段分两组，前端要分开看：
      · 持久事实（来自 `tool_servers` 表）：status / size_bytes / sha256 / 时间
      · 运行事实（来自 MCPManager）：connected / tools / tool_count / 运行期 error
    `status=active` 而 `connected=false` 是**有意义的异常状态**：该跑但没跑起来。
    """

    id: str = Field(description="主键（也是磁盘目录名）")
    name: str = Field(description="机器名，同时也是工具名前缀与 source")
    display_name: str = Field(description="显示名")
    description: str = Field(description="给人看的说明")
    status: str = Field(description="active | disabled | failed")
    connected: bool = Field(description="此刻是否已接入（注册表里有没有它的工具）")
    tools: list[str] = Field(default_factory=list, description="已注册的工具全名（带前缀）")
    tool_count: int = Field(default=0, description="工具数量")
    last_error: str | None = Field(default=None, description="最近一次失败原因（给人看的）")
    size_bytes: int = Field(default=0, description="代码字节数")
    sha256: str = Field(default="", description="代码指纹（判断跑的是不是最新那份）")
    created_at: datetime
    updated_at: datetime


class ToolServerListResponse(BaseModel):
    total: int
    servers: list[ToolServerOut]


class ToolServerUpdate(BaseModel):
    """
    只允许改"意图"，不允许改"结果"。

    `failed` 不在取值里，是刻意的：它是拉起失败**之后**的状态，
    不是用户能设定的目标。想从 failed 恢复，用 active 重试（或 reconnect）。
    """

    status: Literal["active", "disabled"]


class ToolCallRequest(BaseModel):
    """
    「试跑一个已上传的工具」的请求体。

    为什么要有这个能力（不是为了测试方便）：
      工具接进来了、`/api/tools` 里能看见 —— 这只证明**发现**成功。
      真正会出问题的是"参数 schema 对不对、工具内部逻辑跑不跑得通"，
      而这些问题只有**真调一次**才暴露。
      没有这个入口，用户唯一的验证方式是去对话里让 LLM 调 ——
      那要多花一次模型调用，而且模型可能根本不调。
    """

    tool: str = Field(
        min_length=1,
        max_length=100,
        description="工具名。可以是远端原名（text_stats）或全名（demo_tools_text_stats）",
    )
    arguments: dict = Field(
        default_factory=dict,
        description="参数对象（按该工具的 input schema 给）",
    )
