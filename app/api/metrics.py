"""
语义层接口（D37 / PRD §9.10 · F11）
====================================
    GET  /api/metrics          可用指标清单（口径目录：名字 / 中文名 / 说明 / 数据源 / 维度白名单）
    POST /api/metrics/query    按「指标名 + 维度 + 时间窗」查询，返回统一结构的 rows

PRD §11 接口表里的这两个端点。设计要点（都别在本层加料）：

· **路由层不碰 SQL、不碰白名单** —— 校验与渲染全在 metrics_service，
  本层只做两件事：调 service、把 MetricsError 翻译成 400。
  这不是形式主义：Agent 工具（块 3）走的是**同一个 service 入口**，
  校验逻辑若散在两层，"两条调用路径口径一致"就失去保证了。

· **400 而不是 404/422** —— 未知指标 / 维度非法 / 日期非法都是"请求内容问题"，
  用同一个状态码 + 可行动的 detail（消息由 service 措辞：列出可用取值）。
  前端能直接展示、LLM 能据以自我纠正 —— 错误消息是给人（和模型）看的。
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.metrics import MetricInfo, MetricsQueryIn, MetricsQueryOut
from app.services import metrics_service

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_model=list[MetricInfo], summary="可用指标清单（口径目录）")
async def list_metrics() -> list[MetricInfo]:
    """
    列出语义层全部指标的口径信息。

    它是**自描述接口**：调用方（前端 / Agent / 人）不需要读代码或文档，
    从这里就能知道"有哪些指标、每个指标怎么理解、能按什么维度分组"——
    指标定义只有 metrics.yaml 一处，这个端点就是它的只读出口。
    """
    return [MetricInfo.model_validate(item) for item in metrics_service.list_metrics()]


@router.post(
    "/metrics/query",
    response_model=MetricsQueryOut,
    summary="按指标名 + 维度 + 时间窗查询",
)
async def query_metric(body: MetricsQueryIn) -> MetricsQueryOut:
    """
    执行一次指标查询。

    时间窗语义：`[start_date, end_date]` 闭区间，按**北京时间自然日**解释
    （见 metrics_service 模块头的"两条口径约定"）。
    """
    try:
        result = await metrics_service.run_metric(
            metric=body.metric,
            dimension=body.dimension,
            start=body.start_date,
            end=body.end_date,
        )
    except metrics_service.MetricsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return MetricsQueryOut.model_validate(result)
