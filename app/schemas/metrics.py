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
    langfuse 源的行还带 count（样本数）—— P95 这类"分位数"必须能看出样本量。

    degraded / error：**langfuse 源专有的降级标记**（PRD §9.10.5 约束 3）——
    读通道不可用时 rows=[]、degraded=True、error 给一句可读的原因；
    此时 HTTP 仍是 200（"该指标暂不可用"是正常业务状态，不是请求失败），
    前端渲染占位符、Agent 转述原因，两者都按标记行事而不是解析错误。
    sql 源恒为 degraded=False（本地库没有"外部依赖不可用"这种状态）。
    """

    metric: str
    dimension: str | None
    source: str
    rows: list[dict[str, Any]]
    degraded: bool = False
    error: str | None = None


class MetricsOverviewItem(BaseModel):
    """批量查询里的一项：一个指标 + 它的分组维度（与单指标查询同参数）。"""

    metric: str = Field(min_length=1, max_length=64, description="指标名（见 GET /api/metrics）")
    dimension: str | None = Field(
        default=None, max_length=64, description="分组维度（须命中该指标白名单）；聚合指标不传"
    )


class MetricsOverviewIn(BaseModel):
    """
    批量查询请求（D37 补充 / 2026-10-19）。

    为什么要有这个端点：分析页原来并发发 6 个 `/metrics/query`，其中两个
    Langfuse 指标各拉 3 页 = 6 次 HTTP，而 Langfuse v1 observations 端点
    只给 5 次/分钟 —— 必然 429，两个指标双双降级成「暂不可用」。
    合成一个请求后，两个 Langfuse 指标能在**服务端**靠单飞合并成一次取数（3 页）。

    `items` 为什么由调用方给、而不是后端写死六项：
    "分析页要显示哪几张卡"是 UI 决策，不是数据口径；写死在后端会让改版必须动后端。
    后端只负责"批量执行 + 合并取数"，口径仍然全部来自 metrics.yaml。
    """

    start_date: str = Field(description="起始日期 YYYY-MM-DD（北京时间自然日，含当日）")
    end_date: str = Field(description="结束日期 YYYY-MM-DD（北京时间自然日，含当日）")
    items: list[MetricsOverviewItem] = Field(
        min_length=1, max_length=20, description="要查询的指标列表（metric 不可重复）"
    )


class MetricsOverviewOut(BaseModel):
    """
    批量查询结果：`results` 按**指标名**索引，前端直接 `results['token_cost']` 取。

    为什么用 dict 而不是 list：list 会让每个消费者自己按 metric 字段匹配一遍
    （写错名字就是静默 undefined）；dict 取不到就是 undefined，同样的问题但更早暴露，
    而且调用方代码短得多。metric 名因此必须唯一 —— API 层会拒绝重复项。
    """

    start_date: str
    end_date: str
    results: dict[str, MetricsQueryOut]
