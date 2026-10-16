"""
语义层接口的数据结构（D37 / PRD §9.10）
========================================
对应 app/api/metrics.py 的请求/响应体。

一条贯穿全文件的约定：**rows 是"口径的答卷"，形状由 metrics.yaml 定义**——
每行至少含 value（主数值列）；分组查询多一列 dim（分组值，ID）。

日期字段在 schema 层**只做"是字符串"这一层校验**，格式与窗口合法性（YYYY-MM-DD、
start ≤ end）由语义层 metrics_service.parse_window 统一把关 ——
两处都校验会形成"两个口径"，改一处忘一处就是静默不一致（项目里反复踩过的坑型）。
"""

from typing import Any

from pydantic import BaseModel, Field


class MetricInfo(BaseModel):
    """GET /api/metrics 列表项：一个指标的"口径目录"信息。"""

    name: str = Field(description="指标名（查询时用它）")
    label: str = Field(description="中文名")
    description: str = Field(description="口径说明（含分母口径等关键约定）")
    source: str = Field(description="数据源：sql（项目库）/ langfuse_api（运行追踪）")
    dimensions: list[str] = Field(description="可分组维度白名单（空 = 聚合指标，不支持分组）")


class MetricsQueryIn(BaseModel):
    """POST /api/metrics/query 的请求体。"""

    metric: str = Field(min_length=1, max_length=64, description="指标名（见 GET /api/metrics）")
    dimension: str | None = Field(
        default=None,
        max_length=64,
        description="分组维度（须命中该指标白名单）；聚合指标不传",
    )
    start_date: str = Field(description="起始日期 YYYY-MM-DD（北京时间自然日，含当日）")
    end_date: str = Field(description="结束日期 YYYY-MM-DD（北京时间自然日，含当日）")


class MetricsQueryOut(BaseModel):
    """
    查询结果。两种数据源（sql / langfuse_api）返回结构完全一致（PRD §9.10.5 约束 1）。

    rows 的形状：分组查询 `[{"dim": 分组值, "value": 数值, ...}]`；
    聚合指标 `[{"value": 数值, ...}]`（单行、无 dim 列）。
    """

    metric: str
    dimension: str | None
    source: str
    rows: list[dict[str, Any]]
