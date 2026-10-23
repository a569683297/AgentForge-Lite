"""
语义层接口（D37 / PRD §9.10 · F11）
====================================
    GET  /api/metrics          可用指标清单（口径目录：名字 / 中文名 / 说明 / 数据源 / 维度白名单）
    POST /api/metrics/query    按「指标名 + 维度 + 时间窗」查询，返回统一结构的 rows
    POST /api/metrics/overview 批量查询（分析页 6 个指标一次拿全）

PRD §11 接口表里的两个端点。设计要点（都别在本层加料）：

· **路由层不碰 SQL、不碰白名单** —— 校验与渲染全在 metrics_service，
  本层只做两件事：调 service、把 MetricsError 翻译成 400。
  这不是形式主义：Agent 工具（块 3）走的是**同一个 service 入口**，
  校验逻辑若散在两层，"两条调用路径口径一致"就失去保证了。

· **400 而不是 404/422** —— 未知指标 / 维度非法 / 日期非法都是"请求内容问题"，
  用同一个状态码 + 可行动的 detail（消息由 service 措辞：列出可用取值）。
  前端能直接展示、LLM 能据以自我纠正 —— 错误消息是给人（和模型）看的。

· **overview 为什么不复用 6 次单查询**（D37 补充 / 2026-10-19）：
  两个 Langfuse 指标各拉 3 页，并发 6 次 HTTP 超过端点 5 次/分钟的配额，
  两个指标必然被 429 打成「暂不可用」。合成一个请求后，服务端的单飞机制
  （langfuse_client._inflight）能把它们合并成一次 3 页取数 —— 这是前端串行
  做不到的：多标签页、Agent 工具调用各自打一遍时，合并只能发生在取数层。
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.permissions import PERM_METRICS_READ
from app.deps import require_permission
from app.models.user import User
from app.schemas.metrics import (
    MetricInfo,
    MetricsOverviewIn,
    MetricsOverviewOut,
    MetricsQueryIn,
    MetricsQueryOut,
)
from app.services import metrics_service

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_model=list[MetricInfo], summary="可用指标清单（口径目录）")
async def list_metrics(
    user: Annotated[User, Depends(require_permission(PERM_METRICS_READ))],
) -> list[MetricInfo]:
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
async def query_metric(
    body: MetricsQueryIn,
    user: Annotated[User, Depends(require_permission(PERM_METRICS_READ))],
) -> MetricsQueryOut:
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


@router.post(
    "/metrics/overview",
    response_model=MetricsOverviewOut,
    summary="批量查询（分析页一次取全部指标）",
)
async def query_metrics_overview(
    body: MetricsOverviewIn,
    user: Annotated[User, Depends(require_permission(PERM_METRICS_READ))],
) -> MetricsOverviewOut:
    """
    批量执行多个指标查询，结果按指标名索引返回。

    三段式，顺序有讲究：

    ① **先校验全部指标名存在**，再动手执行。
       不这么做的话，`asyncio.gather` 会把所有任务同时启动 —— 第 1 个指标名写错时，
       排在前面的 Langfuse 指标可能**已经把 3 页请求发出去了**，然后整批 400。
       用户的额度被一个拼写错误烧掉，下一次改正重试就正好撞在限流上。
       （维度不在这里预校验：service 层在取数之前就会拒绝非法维度，
        不会浪费任何 HTTP。）

    ② **并发执行**（不是串行）：4 个 SQL 指标互不相干，串行只是白白拉长响应时间。
       两个 Langfuse 指标虽然也并发，但会在 `_fetch_observations` 里被单飞合并成
       一次取数 —— "并发发起、合并取数"正是这层的价值。

    ③ 组装成 dict。metric 名必须唯一（dict 键），重复直接 400 而不是静默覆盖：
       后者会让前端拿到一个"看起来正常、其实是另一个维度"的结果。
    """
    names = [item.metric for item in body.items]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"items 中的 metric 不能重复（结果按指标名索引）：{duplicated}",
        )

    # ① 预校验：任何名字不合法都在发请求之前拦下
    try:
        for item in body.items:
            metrics_service.get_spec(item.metric)
    except metrics_service.MetricsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # ② 并发执行（Langfuse 指标在取数层合并）
    try:
        raw_results = await asyncio.gather(
            *(
                metrics_service.run_metric(
                    metric=item.metric,
                    dimension=item.dimension,
                    start=body.start_date,
                    end=body.end_date,
                )
                for item in body.items
            )
        )
    except metrics_service.MetricsError as exc:
        # 走到这里说明是维度/日期这类执行期才暴露的参数问题（见 ① 的说明）
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # ③ 按指标名索引
    return MetricsOverviewOut(
        start_date=body.start_date,
        end_date=body.end_date,
        results={r["metric"]: MetricsQueryOut.model_validate(r) for r in raw_results},
    )
