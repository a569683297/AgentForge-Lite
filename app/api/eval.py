"""
评测接口（D21）
================
GET /api/eval/cases                  评测集列表（不含参考答案）
GET /api/eval/cases/{case_key}       单条详情（含参考答案与判定依据）
GET /api/eval/dataset                评测集指纹与分布（冻结的可验证出口）

PRD §13.2 里 D21 的验收点就是第一条：**`/api/eval/cases` ok**。

路由层只做三件事：校验查询参数 → 调 eval_service → 用 schema 序列化，
业务逻辑（指纹、写入、统计）全在 app/services/eval_service.py。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.eval import EvalCaseDetailOut, EvalCaseOut, EvalDatasetOut
from app.services import eval_service

router = APIRouter(tags=["eval"])

# 用 Literal 而不是自由字符串：非法类别值由 FastAPI 直接拦成 422 并列出合法取值，
# 不必手写 if 判断 —— 手写漏一个分支时，非法值会**静默查空**返回 `[]`，
# 看起来像"这个类别暂时没有题"，而实际是参数打错了。
CategoryParam = Literal["doc_qa", "cross_doc", "tool_call"]


@router.get("/eval/cases", response_model=list[EvalCaseOut], summary="评测集列表")
async def list_eval_cases(
    category: CategoryParam | None = Query(default=None, description="按类别过滤"),
    limit: int = Query(default=200, ge=1, le=500, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量（分页）"),
) -> list[EvalCaseOut]:
    """
    列出评测用例，按 case_key 排序（顺序稳定，便于与报告逐条对账）。

    响应**不含**参考答案 —— 要看答案走单条详情接口。
    """
    cases = await eval_service.list_cases(category=category, limit=limit, offset=offset)
    return [EvalCaseOut.model_validate(case) for case in cases]


@router.get(
    "/eval/cases/{case_key}",
    response_model=EvalCaseDetailOut,
    summary="单条评测用例详情（含参考答案）",
)
async def get_eval_case(case_key: str) -> EvalCaseDetailOut:
    """
    按业务键取单条用例，含参考答案与判定依据。

    用 case_key（A01/B03）而不是自增 id 做路径参数：
    id 会随评测集重建而变化，报告、命令行、复现说明里写的都是 case_key，
    "第几条"这个说法必须能长期成立（同 D12 用 UUID 而不是可枚举 id 的理由，方向相反但同理）。
    """
    case = await eval_service.get_case_by_key(case_key)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"评测用例不存在：{case_key}")
    return EvalCaseDetailOut.model_validate(case)


@router.get("/eval/dataset", response_model=EvalDatasetOut, summary="评测集指纹与分布")
async def get_eval_dataset() -> EvalDatasetOut:
    """
    返回评测集的内容指纹与分布统计。

    这个端点是"冻结"这条纪律的**可验证出口**：
    D23 报告里会写一个指纹，它必须与这里读到的一致；不一致就说明题目被改过，
    那份报告作废。光有指纹、没有可对照的出口，等于没有冻结。
    """
    stats = await eval_service.dataset_stats()
    return EvalDatasetOut.model_validate(stats)
