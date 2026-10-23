"""
Trace 接口：真实观测数据（Langfuse v2）的只读出口
==================================================
两个接口，对应页面上的两级视图：

    GET /api/traces?hours=24&limit=20   最近 N 条对话（每条是一棵树的汇总）
    GET /api/traces/{trace_id}          某一条的完整 span 树（瀑布图数据源）

为什么不做成"一次返回所有 trace 的所有 span"：
24 小时窗口内可能有几十次对话、几百个 span，一次全塞给前端既慢又没必要 ——
用户一次只看一条。两级各一次请求，配合前端缓存（点过的 trace 不再重复拉）刚好。

降级语义与 `/api/metrics` 保持一致：**读不到观测数据返回 200 + degraded**，
不是 500。观测数据是"锦上添花"的旁路，不让它把整页打成错误页。
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.logging import logger
from app.core.permissions import PERM_TRACE_READ
from app.deps import require_permission
from app.models.user import User
from app.schemas.trace import TraceDetailResponse, TraceListResponse
from app.services import trace_service
from app.services.langfuse_client import LangfuseUnavailableError
from app.services.trace_service import TraceNotFound

router = APIRouter(tags=["trace"])


@router.get(
    "/traces",
    response_model=TraceListResponse,
    summary="最近的 trace 列表（每行 = 一次提问）",
)
async def list_traces_api(
    user: Annotated[User, Depends(require_permission(PERM_TRACE_READ))],
    hours: int = Query(24, ge=1, le=168, description="回看窗口（小时），最大 7 天"),
    limit: int = Query(20, ge=1, le=100, description="最多返回多少条"),
    kind: str | None = Query(
        None,
        description="按类型筛选：dialog（真实对话）/ inspect（检索检查器）/ "
        "eval（评测判分）/ other；不传 = 全部",
    ),
) -> dict:
    """
    按开始时间倒序返回窗口内的 trace 摘要。

    `kind` 的合法值**不在这里硬编码**：真值来源是 `trace_service.KINDS`，
    接口只做透传、非法值由 service 抛 ValueError 再翻成 400 ——
    在这里再写一份 Literal，改一处就会漏一处（参考 RETRIEVER_CONFIGS 的同款取舍）。
    """
    try:
        return await trace_service.list_traces(hours=hours, limit=limit, kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/traces/{trace_id}",
    response_model=TraceDetailResponse,
    summary="一条 trace 的 span 树（真实数据）",
)
async def get_trace_api(
    trace_id: str,
    user: Annotated[User, Depends(require_permission(PERM_TRACE_READ))],
) -> dict:
    try:
        return await trace_service.get_trace(trace_id)
    except TraceNotFound as exc:
        # 404 是**用户问题**（id 抄错 / 超出保留期），不是系统故障 —— 与降级区分开
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LangfuseUnavailableError as exc:
        logger.warning("Trace 详情降级 trace_id=%s 原因=%s", trace_id, exc)
        return {
            "degraded": True,
            "reason": str(exc),
            "trace_id": trace_id,
            "name": "(不可用)",
            "kind": "other",
            "question": None,
            "session_id": None,
            "start_time": datetime.now(timezone.utc),
            "total_ms": 0.0,
            "span_count": 0,
            "spans": [],
        }
