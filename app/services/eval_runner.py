"""
评测执行器（D22）—— 跑一次完整评测：生成 → 打分 → 归因 → 落库
================================================================
这是 D22 交付的那台**机器**：给它一个配置名，它把评测集跑完、逐条打分、
逐条落库、汇总。

--------------------------------------------------------------------------
核心：评测必须拆成两个阶段（先生成、再打分，中间落库）
--------------------------------------------------------------------------
而不是"跑一条、立刻打一条"。这是 D20 那条结论在代码结构上的落地：

    D20 实测出的硬事实 ——「3 次重复」重复的是 **judge（裁判）**，不是生成。
        同一条 B01 题、同配置、两次独立运行 → 459 字判**对** / 511 字判**错**；
        而 judge 内部各自 5 次打分完全一致（[2,2,2,2,2]）。
        **重复打分对"这次答案本身就没生成好"完全无效。**

如果生成与打分写在同一个循环里（跑一次 → 打一次），结构上就**表达不出**
"重复只打在裁判上"这件事。拆开之后，两个次数各有独立的参数：

    generation_runs   同一道题让被测系统跑几次   ← 对抗"生成侧抖动"
    runs_per_case     同一份答案让 judge 打几次   ← 对抗"裁判侧抖动"

拆开还省一大笔钱：

    ┌──────────┬──────────────┬────────────────────────────┐
    │          │ 单次耗时      │ 50 条 × 3 配置              │
    ├──────────┼──────────────┼────────────────────────────┤
    │ 生成     │ 5~10 s       │ 150 次 ≈ 15~25 分钟          │
    │ 打分     │ 0.84 s       │ 450 次 ≈ 6 分钟              │
    └──────────┴──────────────┴────────────────────────────┘
    若不拆、把"3 次重复"套在整条链路上：150 × 3 = 450 次生成 ≈ 45~75 分钟。
    花 3 倍钱，买到的还是同一份答案打 3 次 —— **重复错了对象**。

--------------------------------------------------------------------------
流水线
--------------------------------------------------------------------------
    ① 冻结校验   两个指纹（题 + 语料）必须与报告一致 —— 不一致就拒绝跑
    ② 建 run     先建 eval_runs（拿 run_id），汇总分留空
    ③ 生成       每条题 × generation_runs 次，**每次全新 session、persist=False**
    ④ 打分       每条答案 × runs_per_case 次，取中位数（≡ 多数投票）
    ⑤ 归因       纯函数推导（failure_taxonomy），不让 judge 自由发挥
    ⑥ 落库 + 汇总  明细写入 eval_case_results，汇总回填 eval_runs

--------------------------------------------------------------------------
两个必须写死在代码里的前提
--------------------------------------------------------------------------
    ① 每条题用**全新 session**：
       否则第 2 题会读到第 1 题的问答（`get_window` 命中），答案就真的变了 ——
       而 `persist=False` 只保证"不写"，不保证"读不到别人写的"。
       这是"答案不变"这个论断的**前提条件**，不是顺手为之。
    ② 生成阶段**串行**，打分阶段**并发**：
       生成里包含重排（本地 ONNX，CPU 密集）与 embedding，并发会抢核 ——
       D18/D19 实测过 load 一高延迟就飘（751ms → 1.4s），而这次还要拿延迟当数据。
       打分是"等外部 HTTP"，天然适合并发。
"""

import asyncio
import time
import uuid

from app.config import RETRIEVER_CONFIGS, settings
from app.core.logging import logger
from app.models.eval import EvalCase
from app.services import eval_service, failure_taxonomy, judge_service
from app.services.agent_service import run_agent

# 生成阶段失败时占位的片段文本。judge 的 faithfulness 维度拿它当对照物 ——
# 空字符串会让 judge 无从判断（"答案与空片段比对"是未定义行为），
# 所以显式给一句说明，让 faithfulness 的判定有依据。
_NO_CHUNKS = "（本轮没有检索到任何片段）"


# ============================================================
# 载入题目
# ============================================================
async def load_cases(category: str | None = None, limit: int | None = None) -> list[EvalCase]:
    """按 case_key 顺序载入评测题（顺序稳定 → 输出可复现）。"""
    cases = await eval_service.list_cases(category=category, limit=limit or 500)
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise RuntimeError(
            "评测集是空的（或该类别下没有题）—— 先跑 scripts/d21_seed 落库。"
            "不要跳过这个检查：空评测集会让准确率的**分母为 0**，"
            "而下游很容易把 0/0 当成 0% 报出去。"
        )
    return cases


# ============================================================
# 阶段①：生成
# ============================================================
def _format_chunks(result) -> str:
    """把检索来源拼成 judge 可读的片段文本（与 D20/D21 探针同格式）。"""
    return "\n\n".join(
        f"[{item.index}] 来源：{item.source}\n{item.content}" for item in result.sources
    ) or _NO_CHUNKS


async def generate_answers(
    cases: list[EvalCase],
    *,
    generation_runs: int,
    verbose: bool = True,
) -> list[dict]:
    """
    阶段①：跑被测系统，产出待评答案（**只生成，不打分**）。

    每条题每次生成都用一个全新的 session_id（理由见模块 docstring 的前提①）。
    `persist=False` 不写会话记忆 —— 理由见 `agent_service.run_agent` 的 docstring。

    生成失败**不中断整轮评测**：记为 run_failed，由归因函数判成 system_error。
    这本身是有价值的数据（"几个配置里有没有哪个跑不通"），把它扔掉等于
    把"系统稳定性"从评测里删掉。
    """
    answers: list[dict] = []
    failed = 0
    started = time.perf_counter()

    for case in cases:
        for generation_index in range(generation_runs):
            session_id = uuid.uuid4()
            mark = f"{case.case_key}#{generation_index}"
            try:
                result = await run_agent(session_id, case.question, persist=False)
            except Exception as exc:  # noqa: BLE001 —— 失败也是一条观测
                failed += 1
                logger.warning("生成失败 %s: %s: %s", mark, type(exc).__name__, exc)
                answers.append({
                    "case_key": case.case_key,
                    "category": case.category,
                    "generation_index": generation_index,
                    "answer": "",
                    "retrieved_chunks": _NO_CHUNKS,
                    "tool_calls": [],
                    "sources_count": 0,
                    "run_failed": True,
                    "expected_tool": case.expected_tool,
                    "is_negative": bool(case.is_negative),
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
                if verbose:
                    print(f"    {mark:8s} ❌ {type(exc).__name__}: {str(exc)[:70]}")
                continue

            answers.append({
                "case_key": case.case_key,
                "category": case.category,
                "generation_index": generation_index,
                "answer": result.answer,
                "retrieved_chunks": _format_chunks(result),
                "tool_calls": list(result.tool_calls),
                "sources_count": len(result.sources),
                "run_failed": False,
                "expected_tool": case.expected_tool,
                "is_negative": bool(case.is_negative),
                "error": None,
            })
            if verbose:
                tools = ",".join(result.tool_calls) or "-"
                print(
                    f"    {mark:8s} 答案 {len(result.answer):4d} 字  "
                    f"片段 {len(result.sources):2d} 条  工具 {tools}"
                )

    elapsed = time.perf_counter() - started
    logger.info("生成阶段完成 条数=%d 失败=%d 耗时=%.1fs", len(answers), failed, elapsed)
    if verbose:
        print(f"  [生成] {len(answers)} 份答案（失败 {failed}）耗时 {elapsed:.1f}s")
    return answers


# ============================================================
# 阶段②：打分
# ============================================================
async def _judge_with_semaphore(semaphore: asyncio.Semaphore, **kwargs) -> dict:
    """在并发护栏内打一次分。"""
    async with semaphore:
        return await judge_service.judge_once(**kwargs)


async def score_answers(
    answers: list[dict],
    *,
    judge_model: str,
    runs_per_case: int,
    concurrency: int,
    verbose: bool = True,
) -> list[dict]:
    """
    阶段②：对每份答案重复打分 runs_per_case 次，取中位数，再推导通过与否。

    ----------------------------------------------------------------------
    工具类题**不打分**（D23 修复）
    ----------------------------------------------------------------------
    判据 = `failure_taxonomy.is_tool_only(expected_tool)`（唯一出处，见那里）。

    为什么不打：
        C 类题的 `reference` 是**期望行为说明**而不是标准答案（出题时就写明了的），
        但 `judge_service.build_prompt` 无差别地把它标成【参考答案】发出去，
        judge 于是判"答案没按要求执行"给 0 分。
        D23 实测：15 条失败里 13 条是这么来的（C05「现在几点了？」
        **答案正确、工具调对，judge 给 0 分**）。

        改判定路径（而不是改写 reference）的理由见 failure_taxonomy 的 docstring。
        这里补一条**成本**理由：不打分还省掉 C 类题的全部 judge 调用
        （30 行 × 3 次 = 90 次 / 每轮约 28 万 token），并且让 C 类题的结果
        **不再有裁判侧抖动** —— 它变成一个确定性判定。

    ⚠ 打分**全部失败**时抛错，而不是落库成"答错"。
      理由：那会让"量具坏了"伪装成"系统答错了"，而两者的修法完全不同
      （一个修 judge 通道，一个改检索/prompt）。这正是 D21 那条教训的形态 ——
      静默地把一种情况算成另一种。
      只失败一部分时保留（用成功的那几次算），但 `judge_runs` 会如实记下次数，
      验证脚本据此能看出"这条数据不满次数"。
    """
    semaphore = asyncio.Semaphore(concurrency)
    tasks: list[list[asyncio.Task]] = []
    # ⚠ 计时起点必须在被测量区间**之内** —— 放在 gather 之后会算出 0.0s
    #   （D20 踩过同一条坑的反面：那次起点在并发锁外面，把排队时间混进了单次耗时，
    #    把 0.84s 报成 11.74s；这次方向相反，会把耗时报成 0）。
    #   两种错法都不会报错，只会给出一个"看起来很正常"的假数。
    started = time.perf_counter()

    skipped = 0
    for row in answers:
        if row["run_failed"]:
            tasks.append([])     # 没跑出答案 → 不打分（归因会直接判 system_error）
            continue
        if failure_taxonomy.is_tool_only(row["expected_tool"]):
            # 工具类题：判定只看工具调用，内容不进 judge。
            # 用空 list 占位（而不是 continue），下面的 zip 才能按位置对齐。
            tasks.append([])
            skipped += 1
            continue
        tasks.append([
            asyncio.create_task(_judge_with_semaphore(
                semaphore,
                question=_question_of(row),
                reference=row["reference"],
                chunks=row["retrieved_chunks"],
                answer=row["answer"],
                judge_model=judge_model,
            ))
            for _ in range(runs_per_case)
        ])

    all_results = await asyncio.gather(*[t for group in tasks for t in group])
    # 把扁平结果按原来的分组切回去（顺序与 create_task 的顺序一致）
    cursor = 0
    grouped: list[list[dict]] = []
    for group in tasks:
        grouped.append(list(all_results[cursor:cursor + len(group)]))
        cursor += len(group)

    scored: list[dict] = []

    for row, results in zip(answers, grouped):
        if row["run_failed"]:
            passed, reason = failure_taxonomy.derive_case_outcome(run_failed=True)
            c = f = m = None
            runs, raw = 0, None
        elif not results:
            # 工具类题：没有分数，也**不需要**分数（derive 会走工具那条路）
            c = f = m = None
            runs, raw = 0, None
            passed, reason = failure_taxonomy.derive_case_outcome(
                correctness=None, faithfulness=None, completeness=None,
                expected_tool=row["expected_tool"],
                actual_tools=row["tool_calls"],
            )
        else:
            outcome = judge_service.aggregate(results)
            if outcome.all_failed:
                raise RuntimeError(
                    f"{row['case_key']}#{row['generation_index']} 的 {len(results)} 次打分**全部失败**："
                    f"{outcome.errors[:2]}。"
                    "本轮评测中止 —— 把打分失败落库成'答错'会让量具故障伪装成系统缺陷，"
                    "而两者需要完全不同的修复。"
                )
            c, f, m = outcome.correctness, outcome.faithfulness, outcome.completeness
            runs = outcome.runs_ok
            # 原始各次分数：D20 的教训是"报平均分等于在比噪声更小的差异"，
            # 存下每一次才能事后算抖动、判断某个差异是否超出门槛
            raw = list(outcome.raw)
            passed, reason = failure_taxonomy.derive_case_outcome(
                correctness=c, faithfulness=f, completeness=m,
                expected_tool=row["expected_tool"],
                actual_tools=row["tool_calls"],
            )

        scored.append({
            **row,
            "score_correctness": c,
            "score_faithfulness": f,
            "score_completeness": m,
            "judge_runs": runs,
            "judge_raw": raw,
            "passed": passed,
            "failure_reason": reason,
        })

    elapsed = time.perf_counter() - started
    ok_runs = sum(r.get("judge_runs", 0) for r in scored)
    if verbose:
        print(
            f"  [打分] {ok_runs} 次成功（期望 {len(answers) * runs_per_case} 次，"
            f"其中 {sum(1 for a in answers if a['run_failed'])} 份未生成、"
            f"{skipped} 份工具类题不打分）耗时 {elapsed:.1f}s"
        )
    return scored


# ============================================================
# 主流程
# ============================================================
def _question_of(row: dict) -> str:
    """
    从明细行取题面。

    ⚠ 明细行里**不存 question**（它属于 eval_cases，冗余一份会变成第二处真相），
      所以题面由 `run_evaluation` 在生成之后、打分之前临时挂在行上
      （键名 `_question` 带下划线前缀，`_registration_row` 落库时会把它剔除）。
      `reference` 同理 —— 它也是打分要用、但不该写进明细表的东西。
    """
    return row["_question"]


def _registration_row(row: dict) -> dict:
    """剥掉内部字段，产出可直接落库的明细行（key 必须与 _REQUIRED_RESULT_FIELDS 对齐）。"""
    return {
        "case_key": row["case_key"],
        "category": row["category"],
        "generation_index": row["generation_index"],
        "answer": row["answer"],
        "retrieved_chunks": row["retrieved_chunks"],
        "tool_calls": row["tool_calls"],
        "sources_count": row["sources_count"],
        "score_correctness": row["score_correctness"],
        "score_faithfulness": row["score_faithfulness"],
        "score_completeness": row["score_completeness"],
        "judge_runs": row["judge_runs"],
        "judge_raw": row["judge_raw"],
        "passed": row["passed"],
        "failure_reason": row["failure_reason"],
    }


async def run_evaluation(
    config_name: str,
    *,
    generation_runs: int = 1,
    runs_per_case: int | None = None,
    judge_model: str | None = None,
    category: str | None = None,
    limit: int | None = None,
    verbose: bool = True,
) -> dict:
    """
    跑一次完整评测并落库。返回汇总 dict。

    Args:
        config_name:    检索配置 pure_vector / hybrid / hybrid_rerank（消融的自变量）
        generation_runs: 每条题重复生成次数（D22 默认 1；D23 用探针数据决定）
        runs_per_case:  每条答案重复打分次数（默认 settings.judge_runs_per_case = 3）
        judge_model:    打分模型通道名（默认 settings.judge_model = deepseek）
        category:       只跑某一类题（调试用；D23 的正式消融应跑全部）
        limit:          只跑前 N 条（调试用）

    ------------------------------------------------------------------
    配置切换：**运行时改 settings，跑完恢复**
    ------------------------------------------------------------------
    工具层的 `retrieve()` 从 `settings.retriever_config` 读配置，所以切换只需改这个值。

    ⚠ 但 pydantic 的 BaseSettings **默认不校验赋值**（validate_assignment=False），
      也就是说 `settings.retriever_config = "hybrid_rerankk"`（多打一个 k）
      **不会报错**，而是静默地让检索走默认分支 —— 那一列的配置就是假的。
      所以这里必须**显式校验**，不能指望配置层那个 field_validator
      （它只在构造 Settings 时跑，管不到赋值）。

    ⚠ 改全局单例状态在并发服务里是危险的（会影响同进程内的其它请求）。
      对"离线批量评测"可以接受，但**跑完必须恢复**，否则后续手动对话
      会莫名其妙用了最后一档配置 —— 而那不会有任何提示。
    """
    if config_name not in RETRIEVER_CONFIGS:
        raise ValueError(
            f"未知检索配置 {config_name!r}，可选：{RETRIEVER_CONFIGS}"
            "（不能靠配置层的 validator 兜：它管不到赋值）"
        )

    runs_per_case = runs_per_case or settings.judge_runs_per_case
    judge_model = judge_model or settings.judge_model

    # ---- ① 冻结校验：两个指纹都要与当前库一致 ----
    dataset_fingerprint, case_count = await eval_service.fingerprint_from_db()
    corpus_fingerprint, chunk_count = await eval_service.corpus_fingerprint_from_db()

    cases = await load_cases(category=category, limit=limit)
    if limit is None and category is None and len(cases) != case_count:
        # 只有"跑全量"时才是硬错误；带 limit/category 的调试跑本来就会少跑。
        # 少了题而无人察觉，会让准确率的**分母**悄悄变小 —— 分数看起来更漂亮。
        raise RuntimeError(
            f"载入 {len(cases)} 条但库里共 {case_count} 条 —— 载入过程丢了题，"
            "此时算出的准确率分母是错的"
        )

    if verbose:
        print(f"  检索配置   : {config_name}")
        print(f"  评测集指纹 : {dataset_fingerprint[:16]}…  题数 {case_count}")
        print(f"  语料指纹   : {corpus_fingerprint[:16]}…  切片 {chunk_count}")
        print(f"  生成次数   : {generation_runs}   打分次数 : {runs_per_case}   judge : {judge_model}")
        print(f"  本次用题   : {len(cases)} 条")

    # ---- ② 建 run（先建，明细才能带 run_id 逐批落库）----
    run_id = await eval_service.create_run(
        config_name=config_name,
        dataset_fingerprint=dataset_fingerprint,
        corpus_fingerprint=corpus_fingerprint,
        judge_model=judge_model,
        generation_runs=generation_runs,
        runs_per_case=runs_per_case,
    )

    # ---- ③④⑤ 生成 → 打分 → 归因（切换检索配置期间执行）----
    previous_config = settings.retriever_config
    settings.retriever_config = config_name
    try:
        if verbose:
            print(f"\n  ── 阶段① 生成（{len(cases)} 题 × {generation_runs} 次，串行）──")
        answers = await generate_answers(cases, generation_runs=generation_runs, verbose=verbose)
        # 把题面塞进每一行（judge 需要题面；但题面不落库，故用下划线前缀的内部键）
        question_by_key = {case.case_key: case.question for case in cases}
        reference_by_key = {case.case_key: case.reference for case in cases}
        for row in answers:
            row["_question"] = question_by_key[row["case_key"]]
            row["reference"] = reference_by_key[row["case_key"]]

        if verbose:
            print(f"\n  ── 阶段② 打分（{len(answers)} 份答案 × {runs_per_case} 次，并发 {settings.judge_max_concurrency}）──")
        scored = await score_answers(
            answers,
            judge_model=judge_model,
            runs_per_case=runs_per_case,
            concurrency=settings.judge_max_concurrency,
            verbose=verbose,
        )
    finally:
        # 无论成功失败都要恢复 —— 否则后续手动对话会静默用最后一档配置
        settings.retriever_config = previous_config

    # ---- ⑥ 落库 + 汇总 ----
    written = await eval_service.insert_case_results(
        run_id, [_registration_row(row) for row in scored]
    )
    summary = eval_service.summarize_results(scored)
    await eval_service.finish_run(run_id, summary)

    result = {
        "run_id": run_id,
        "config_name": config_name,
        "dataset_fingerprint": dataset_fingerprint,
        "corpus_fingerprint": corpus_fingerprint,
        "judge_model": judge_model,
        "generation_runs": generation_runs,
        "runs_per_case": runs_per_case,
        "rows_written": written,
        **summary,
    }
    logger.info(
        "评测完成 run_id=%d config=%s accuracy=%.4f 明细=%d 条",
        run_id, config_name, summary["accuracy"], written,
    )
    return result
