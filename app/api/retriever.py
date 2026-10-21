"""
检索检查器（RetrieverPage）的 HTTP 出口。

只做三件事：参数校验交给 pydantic、异常翻译成状态码、把 service 的返回值原样送出。
检索逻辑一律在 `app/services/retriever_service.py`，这里不写业务判断 ——
否则"页面看到的"和"评测算的"会各自长出一套口径。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import RETRIEVER_CONFIGS, settings
from app.schemas.retriever import (
    MAX_TOP_K,
    RetrieverOptionsOut,
    RetrieverSearchRequest,
    RetrieverSearchResponse,
)
from app.services import retriever_service

router = APIRouter(tags=["retriever"])


@router.get(
    "/retriever/options",
    response_model=RetrieverOptionsOut,
    summary="检索检查器的可选项（配置清单 / 默认值 / k 上限）",
)
async def get_options() -> RetrieverOptionsOut:
    """
    把"有哪些配置可选"和"k 能填多大"由后端说出来，而不是前端写死。

    为什么值得单开一个接口：这三样东西各自都有硬约束对端 ——
    配置名必须与 `RETRIEVER_CONFIGS` 一致、`max_top_k` 必须等于候选池宽度、
    默认值来自 `settings.retriever_config`。前端硬编码一份，后端改一个，
    页面就会拿着一个不存在的配置名去请求，得到 400 而不是"选项里没有它"。
    """
    return RetrieverOptionsOut(
        configs=list(RETRIEVER_CONFIGS),
        default_config=settings.retriever_config,
        max_top_k=MAX_TOP_K,
        default_top_k=5,
    )


@router.post(
    "/retriever/search",
    response_model=RetrieverSearchResponse,
    summary="跑一次检索并返回取舍过程（候选池 → 融合 → 重排 → gold 判定）",
)
async def search(req: RetrieverSearchRequest) -> RetrieverSearchResponse:
    """
    这个接口**不是** Agent 用的检索入口（那是 `query_metric` / `search_documents` 工具
    背后的 `retrieve()`），它只服务页面：给候选池、给两阶段名次、给 gold 比对。

    三种失败都翻译成 4xx 而不是 500 —— 它们全是可以照着提示改的输入问题，
    500 会让人以为是服务坏了：

    | 情况 | 状态码 | 页面该怎么提示 |
    |---|---|---|
    | 配置名不在白名单 | 400 | 下拉里选一个（正常走不到） |
    | 题号不存在 | 404 | 题库刷新后那条题被删了 |
    | 知识库为空 | 200 | 不是错误：`candidates=[]`，页面显示"库里没有可检索内容" |
    """
    try:
        data = await retriever_service.search_debug(
            query=req.query,
            top_k=req.top_k,
            config=req.config,
            case_key=req.case_key,
        )
    except ValueError as exc:
        # 只有 config 非法会走到这里（pydantic 的 Literal 已拦住其余取值）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail=f"评测题 {exc} 不存在（题号可能已随题库更新而删除）"
        ) from exc

    return RetrieverSearchResponse(**data)
