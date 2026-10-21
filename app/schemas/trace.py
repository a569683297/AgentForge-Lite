"""
Trace 接口的响应模型
====================
字段命名统一 snake_case（与项目其他接口一致），前端拿到后自己转驼峰。

两个刻意的设计：
  · `degraded` 不做成 HTTP 错误码 —— 观测数据读不到时页面仍应能打开，
    并明确告诉用户"为什么没有数据"。把它做成 500 会让前端只剩一个红条。
  · `start_ms` / `end_ms` 是**相对本次 trace 起点**的毫秒，不是时间戳。
    瀑布图的横轴只关心"谁先开始、谁持续多久"，绝对值留在 `start_time` 里。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class TraceSpan(BaseModel):
    """树里的一个节点。顺序即渲染顺序（父一定在子之前）。"""

    id: str = Field(description="observation id")
    parent_id: str | None = Field(default=None, description="父 observation id；根为 null")
    name: str = Field(description="span 名，如 run_agent / tool:search_documents")
    type: str = Field(description="AGENT | GENERATION | TOOL | SPAN | RETRIEVER | EMBEDDING")
    depth: int = Field(description="层级，0 = 根；前端用它做缩进")
    start_ms: float = Field(description="相对本次 trace 起点的开始毫秒")
    end_ms: float = Field(description="相对本次 trace 起点的结束毫秒")
    duration_ms: float = Field(description="耗时毫秒（优先用 Langfuse 记录的 latency）")
    status: str = Field(
        description=(
            "ok | warning | error。"
            "warning = 有 statusMessage 或是 WARNING 级（典型场景：重排超时降级），"
            "**不等于失败** —— 页面用黄色区分，别和红色错误混着看"
        )
    )
    status_message: str | None = Field(
        default=None,
        description="Langfuse 记的原因，如「重排超时降级（返回融合序）」；无则 null",
    )
    model: str | None = Field(default=None, description="generation 类才有，v2 里叫 modelId")


class TraceSummary(BaseModel):
    """列表页一行 —— 就是「一次提问」的抬头。不建树，所以只给汇总。"""

    trace_id: str
    name: str = Field(description="根 span 名，通常就是 run_agent")
    kind: str = Field(
        description="dialog（真实对话）/ inspect（检索检查器）/ eval（评测判分）/ other"
    )
    question: str | None = Field(
        default=None,
        description=(
            "这次提问的原文。来自 messages.trace_id 反查；"
            "None = 该次早于 trace 记录（历史对话）或评测运行（persist=False 不落库）"
        ),
    )
    session_id: str | None = Field(default=None, description="对应的项目会话 id")
    start_time: datetime
    duration_ms: float
    span_count: int
    has_error: bool


class TraceListResponse(BaseModel):
    degraded: bool = Field(description="true = Langfuse 读通道不可用，trace 列表为空")
    reason: str | None = Field(default=None, description="降级原因（人话）")
    window_hours: int
    fetched_at: datetime
    truncated: bool = Field(
        description="true = 窗口内 span 超过单次上限，只统计了最近一批，数字偏小"
    )
    kind_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "本窗口内各类型的条数（**按 kind 过滤前**统计）。"
            "筛选器靠它显示「对话 3 / 检索检查 17」，没有的类不出现、前端按 0 处理"
        ),
    )
    traces: list[TraceSummary]


class TraceDetailResponse(BaseModel):
    degraded: bool
    reason: str | None = None
    trace_id: str
    name: str
    kind: str = Field(description="同 TraceSummary.kind")
    question: str | None = Field(default=None, description="这次提问的原文；None = 未记录")
    session_id: str | None = None
    start_time: datetime
    total_ms: float = Field(description="整条 trace 的墙钟跨度，瀑布图横轴上限")
    span_count: int
    spans: list[TraceSpan]
