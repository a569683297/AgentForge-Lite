"""
评测接口（D21 起）
====================
D21 —— 评测集本身
    GET /api/eval/cases                  评测集列表（不含参考答案）
    GET /api/eval/cases/{case_key}       单条详情（含参考答案与判定依据）
    GET /api/eval/dataset                评测集指纹与分布（冻结的可验证出口）

D22 —— 评测运行与明细
    GET /api/eval/runs                   运行列表（最新在前）
    GET /api/eval/runs/{run_id}          运行详情（含按类别聚合 + 失败模式分布）
    GET /api/eval/runs/{run_id}/cases    本次运行的明细（可按类别 / 通过与否过滤）
    GET /api/eval/runs/{run_id}/results/{case_key}
                                         某题本次的全部生成（含检索片段与原始分数）

PRD §13.2 里两天的验收点分别是：D21 = **`/api/eval/cases` ok**；
D22 = **单条可评分 + 明细表有行、按类别可聚合** —— 后两者由 `/runs/{id}` 与
`/runs/{id}/cases` 给出可读证据（"有行"不是靠 SQL 手敲一遍声称的）。

路由层只做三件事：校验查询参数 → 调 service → 用 schema 序列化，
业务逻辑（指纹、写入、统计、聚合）全在 app/services/ 下。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.eval import (
    EvalCaseDetailOut,
    EvalCaseOut,
    EvalCaseResultDetailOut,
    EvalCaseResultOut,
    EvalDatasetOut,
    EvalRunDetailOut,
    EvalRunOut,
)
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


# ============================================================
# D22：评测运行与明细
# ============================================================
@router.get("/eval/runs", response_model=list[EvalRunOut], summary="评测运行列表")
async def list_eval_runs(
    limit: int = Query(default=50, ge=1, le=200, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量（分页）"),
) -> list[EvalRunOut]:
    """
    列出评测运行，最新创建的在前。

    每条都带齐"可比性四件套"（配置 / 题指纹 / 语料指纹 / judge 与重复次数）——
    少了任何一个，这个分数都不能与别的 run 直接比。
    """
    runs = await eval_service.list_runs(limit=limit, offset=offset)
    return [EvalRunOut.model_validate(run) for run in runs]


@router.get("/eval/runs/{run_id}", response_model=EvalRunDetailOut, summary="运行详情（含按类别聚合）")
async def get_eval_run(run_id: int) -> EvalRunDetailOut:
    """
    运行详情：汇总分 + **按类别聚合** + 失败模式分布 + 生成侧不一致题数。

    注意三个口径不同（别放在一起比）：
        `accuracy`        分母是**题数**（每题多次生成先多数投票）
        `scored_rows`     三维度均分的分母（**有分数的行数**，不等于 rows）
        `by_category` 里的 avg_*   分母是该类别里**有分数的行数**
    这一点在 schema 的字段说明里也写了 —— 报告必须写明分母，
    否则 "+10%" 到底是 50 条里的 5 条还是 150 行里的 15 行就说不清。
    """
    run = await eval_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"评测运行不存在：{run_id}")

    # 汇总从**明细重算**，而不是读 eval_runs 上那几个字段。
    # 两条路径应当给出同一个结果（验证脚本有一条断言钉住它）——
    # 若不重算，接口就只能展示落库时算的那份，一旦那时的算法有 bug，
    # 接口会忠实地把错数字一直展示下去。
    rows = await eval_service.list_case_results(run_id, limit=2000)
    summary = eval_service.summarize_results(rows)
    by_category = await eval_service.aggregate_by_category(run_id)

    base = EvalRunOut.model_validate(run).model_dump()
    return EvalRunDetailOut(
        **base,
        by_category=by_category,
        failure_breakdown=summary["failure_breakdown"],
        rows=summary["total_rows"],
        scored_rows=summary["scored_rows"],
        generation_inconsistent=summary["generation_inconsistent"],
    )


@router.get(
    "/eval/runs/{run_id}/cases",
    response_model=list[EvalCaseResultOut],
    summary="运行明细（可按类别 / 通过与否过滤）",
)
async def list_eval_run_cases(
    run_id: int,
    category: CategoryParam | None = Query(default=None, description="按类别过滤"),
    passed: bool | None = Query(default=None, description="只看通过 / 只看失败"),
    limit: int = Query(default=500, ge=1, le=2000, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量（分页）"),
) -> list[EvalCaseResultOut]:
    """
    本次运行的明细行 —— 这就是 PRD 验收点里"明细表有行"的可读证据。

    一行 = 一道题的**一次生成**（generation_runs > 1 时同一题会出现多行）。
    响应**不含** retrieved_chunks（几千字，列表接口带上会让响应膨胀到几百 KB）；
    要看检索片段走 `/runs/{run_id}/results/{case_key}`。
    """
    run = await eval_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"评测运行不存在：{run_id}")

    rows = await eval_service.list_case_results(
        run_id, category=category, passed=passed, limit=limit, offset=offset
    )
    return [EvalCaseResultOut.model_validate(row) for row in rows]


@router.get(
    "/eval/runs/{run_id}/results/{case_key}",
    response_model=list[EvalCaseResultDetailOut],
    summary="某题在本次运行里的全部生成（含检索片段与原始分数）",
)
async def get_eval_run_result(run_id: int, case_key: str) -> list[EvalCaseResultDetailOut]:
    """
    取某一道题在本次运行里的**全部生成**（不是只取一条）。

    为什么返回列表：generation_runs > 1 时，"这道题飘不飘"正是要靠
    **同一题的多次生成放在一起**才能看出来（D21 实测的生成侧抖动就是这样被发现的）。
    只返回"第一条"会把最有信息量的那部分丢掉。
    """
    run = await eval_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"评测运行不存在：{run_id}")

    rows = await eval_service.list_case_results(run_id, limit=2000)
    picked = [row for row in rows if row.case_key == case_key]
    if not picked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"本次运行里没有题目 {case_key} 的明细（运行 {run_id}）",
        )
    return [EvalCaseResultDetailOut.model_validate(row) for row in picked]
