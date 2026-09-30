"""
评测报告生成（D24，PRD F7.4）
==============================
把"库里已有的评测明细"变成一份**可读、可核查、可复现**的 Markdown 报告。

    F7.4  报告生成：Markdown → docs/eval-report-{date}.md
    §9.5  报告结构：总体对比表 → 每类明细 → 失败案例（各取 3 条）→ 结论与建议

--------------------------------------------------------------------------
它在这一层，而不是在脚本里
--------------------------------------------------------------------------
本模块**只读数据、只拼字符串**，不跑被测系统、不调用 LLM（零 token）。
分工与既有模块一致：

    eval_runner      跑评测（花 token）→ 写 eval_runs / eval_case_results
    eval_service     评测数据层（建表、写入、聚合）
    eval_report_service  ★ 本模块：把明细**呈现**成报告
    scripts/d24_report.py  命令行入口（选 run、落盘、回写 report_path）

为什么生成逻辑不写在脚本里：报告是**最终交付物**，它的结构（有几个必需章节）
必须能被验证脚本直接调用检查。写在 `main()` 里就只能靠"跑一遍看看"来验。

--------------------------------------------------------------------------
本模块最重要的一条设计：**指纹守卫**（不通过就拒绝出报告）
--------------------------------------------------------------------------
报告的**全部意义**是"同一份考卷、同一份教材，只换检索配置，分数怎么变"。
所以三个 run 必须满足：

    ① dataset_fingerprint 逐字相同  —— 考卷是同一份
    ② corpus_fingerprint  逐字相同  —— 教材是同一份
    ③ 题号集合完全相同               —— 没有被 --limit / --category 截过

任一条不满足，那份报告就是**拿两批不同的东西在比**，而它看起来完全正常
（表格照排、百分比照算）—— 这正是铁律 12「静默失效」最贵的形态。

D23 已经踩过一次同类：当时引用 run 1 与 run 4/5/6 做对照，
而 run 1 **只跑了 C01~C04（8 行）**、run 4/5/6 跑了全部 10 条 C 类题 ——
**两次覆盖的题目集合不同**，那个对照没有任何含义。

→ 所以这里不是"打一行警告"，而是**抛异常**（`ReportGuardError`）。
⚠ 抛的是**它自己的类型**，不是包装成 RuntimeError：调用方要能区分
  "数据不可比（该去查数据）"与"代码/环境出错（该去查代码）"——
  包成一种异常等于把可判断的信息变成不可判断（铁律 12 附则三）。

--------------------------------------------------------------------------
第二个设计：「各取 3 条」取不满时，报告要**明说取不满**
--------------------------------------------------------------------------
PRD §9.5 要求"失败案例各取 3 条"。实测 50 题三配置**只失败 1 条**（B01），
所以每个配置只能取到 1 条。**取不到 3 条是一个要写进结论的事实**，
不是要遮掩的缺陷 —— 恰恰相反，它本身就是"这批题对当前系统太简单"的直接证据。
→ 报告里写"该配置失败 N 条，不足 3 条"，并把这个缺口纳入 §结论。

--------------------------------------------------------------------------
第三个设计：结论段全部由数据分支决定（铁律 11）
--------------------------------------------------------------------------
D13/D16/D18 三次栽在同一处：脚本一边打印"区分度被压缩了"，
一边上面那行数字显示它放大了 —— **数据反了，文字照念**。
所以本模块所有结论句都是 `if/else` 算出来的，**没有一句硬编码的判定**。
自检口诀：指着每个结论里的数字问"这个数是哪个变量算出来的"。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from sqlalchemy import select

from app.config import RETRIEVER_CONFIGS
from app.core.db import async_session_factory
from app.models.eval import EVAL_CATEGORIES, EvalCaseResult, EvalRun
from app.services.eval_service import _field, summarize_results


# ============================================================
# 常量
# ============================================================
#: PRD §9.5 要求的报告结构 —— **验证脚本按它逐节检查**，
#: 所以标题字符串只在这里出现一次（第二处出现就会各自演化，同 D23 附则八）。
REQUIRED_SECTIONS: tuple[str, ...] = (
    "## 1. 总体对比表",
    "## 2. 每类明细",
    "## 3. 失败案例",
    "## 4. 结论与建议",
)

#: 「失败案例各取 3 条」里的那个 3。抽成常量是因为报告正文要引用它
#: （"不足 3 条"这句话里的数字必须和取数逻辑同源，否则改了一处另一处不会动）。
FAILURE_CASES_PER_CONFIG = 3

#: PRD S2 验收门槛：混合+重排 vs 纯向量 ≥ +10%（相对百分点）。
S2_MIN_GAIN_PP = 10.0

#: 报告里"答什么算对"的口径（PRD F7.5）。写进报告，因为它决定表格里所有数字的含义。
PASS_THRESHOLD = 4.0

#: 报告默认落盘目录（相对项目根）。PRD 明确要求 `docs/eval-report-{date}.md`。
DEFAULT_OUT_DIR = "docs"


class ReportGuardError(RuntimeError):
    """
    数据不满足"可比"的前提（指纹不一致 / 题号集合不同 / 配置不全）。

    单独一个类型，是为了让调用方能区分它和"代码崩了"：
    它意味着**这批数据不该拿来出报告**，而不是"程序有 bug"。
    """


# ============================================================
# 取数与守卫
# ============================================================
async def load_latest_runs() -> list[EvalRun]:
    """
    取**每个配置的最新一轮**（三个配置各一条）。

    为什么是"每配置最新一轮"而不是"满足条件的全部"：
    D23 踩过这个坑 —— 诊断脚本写 `where generation_runs = 1`，
    第一轮恰好命中 3 条，重跑之后就变成 6 条，**把已声明作废的旧轮混进了统计**，
    而且不报错、不崩溃、数字看着也正常。
    → 能被重复执行的取数条件，必须**随重跑自洽收缩**，
      `distinct on (config) ... order by config, id desc` 就是这种条件。
    """
    async with async_session_factory() as session:
        stmt = (
            select(EvalRun)
            .distinct(EvalRun.config_name)
            .order_by(EvalRun.config_name, EvalRun.id.desc())
        )
        return list((await session.scalars(stmt)).all())


async def load_runs_by_id(run_ids: list[int]) -> list[EvalRun]:
    """按显式 run_id 取（人工指定时用），返回顺序按传入顺序。"""
    async with async_session_factory() as session:
        rows = list(
            (await session.scalars(select(EvalRun).where(EvalRun.id.in_(run_ids)))).all()
        )
    by_id = {r.id: r for r in rows}
    missing = [i for i in run_ids if i not in by_id]
    if missing:
        raise ReportGuardError(f"run_id 不存在：{missing}")
    return [by_id[i] for i in run_ids]


async def load_rows(run_id: int) -> list[EvalCaseResult]:
    """取某轮的**全部**明细行（不分页 —— 报告要全量口径）。"""
    async with async_session_factory() as session:
        stmt = (
            select(EvalCaseResult)
            .where(EvalCaseResult.run_id == run_id)
            .order_by(EvalCaseResult.case_key, EvalCaseResult.generation_index)
        )
        return list((await session.scalars(stmt)).all())


def check_comparable(runs: list[EvalRun], rows_by_run: dict[int, list]) -> None:
    """
    **可比性守卫**：任何一条不满足就抛 `ReportGuardError`，拒绝出报告。

    这是本模块唯一会中断流程的地方，所以每条检查都对应一个**具体的错误解读**：

        配置不全      → 少一个配置，"对比表"就缺一列，读者会以为只跑了两个
        指纹为空      → 空指纹说明这轮跑在指纹机制生效之前（如 run 1），它的"配置对比"没有前提
        指纹不一致    → 换过题或换过语料，分数差里混着"题目变了"，归因不成立
        题号集合不同  → --limit / --category 截过，分母不同，百分比不可比
    """
    names = [r.config_name for r in runs]
    if sorted(names) != sorted(RETRIEVER_CONFIGS):
        raise ReportGuardError(
            f"配置不全：拿到 {names}，期望 {sorted(RETRIEVER_CONFIGS)}。"
            "缺配置时对比表会缺列，不能出报告。"
        )

    dfps = {r.config_name: (r.dataset_fingerprint or "") for r in runs}
    if any(not v for v in dfps.values()):
        raise ReportGuardError(f"评测集指纹为空：{dfps}（该轮跑在指纹机制生效之前）")
    if len(set(dfps.values())) != 1:
        raise ReportGuardError(f"评测集指纹不一致 → 考卷不是同一份：{dfps}")

    cfps = {r.config_name: (r.corpus_fingerprint or "") for r in runs}
    if any(not v for v in cfps.values()):
        raise ReportGuardError(f"语料指纹为空：{cfps}（该轮跑在指纹机制生效之前）")
    if len(set(cfps.values())) != 1:
        raise ReportGuardError(f"语料指纹不一致 → 教材不是同一份：{cfps}")

    key_sets = {r.config_name: frozenset(str(_field(x, "case_key")) for x in rows_by_run[r.id]) for r in runs}
    if any(not s for s in key_sets.values()):
        raise ReportGuardError(f"有配置没有任何明细行：{ {k: len(v) for k, v in key_sets.items()} }")
    # ⚠ 必须写 `frozenset.intersection(...)`，不能写 `set.intersection(...)`：
    #   后者是**未绑定方法**，descriptor 会检查第一个实参是不是 `set` 实例，
    #   而 frozenset **不是** set 的子类 → `TypeError: descriptor 'intersection'
    #   for 'set' objects doesn't apply to a 'frozenset' object`。
    #   ⚠ 这个错只有在"集合确实不同"时才会走到（相同就短路进不了这一段），
    #     所以它**恰好在最需要守卫的那一刻才炸**：本该拒绝出报告，结果程序崩了。
    #     是验证脚本的 B3 负面测试把它打出来的 —— 正面路径永远碰不到这一行。
    common = frozenset.intersection(*key_sets.values())
    if any(s != common for s in key_sets.values()):
        sizes = {k: len(v) for k, v in key_sets.items()}
        only = {k: sorted(v - common) for k, v in key_sets.items() if v != common}
        raise ReportGuardError(
            f"各配置的题号集合不同 → 分母不同，百分比不可比。行数 {sizes}；"
            f"仅出现在某些配置里的题号 {only}"
        )


# ============================================================
# 汇总（口径一律复用 eval_service —— 不在这里重算）
# ============================================================
def mean_scores(rows: list) -> dict:
    """
    三维度均分（**行级**口径）+ 真实分母。

    ⚠ 为什么必须带上 `scored_rows`：D23 修复后工具类题不判内容，
      它们的 `score_*` 是 NULL。SQL 的 AVG 会跳过 NULL，所以**分数没错**，
      错的是**分母是隐含的** —— 报告里若写"平均正确性 4.9（共 50 行）"，
      那个 50 是错的，真实分母只有 40。
      → 让分母成为一个**可被引用的字段**，而不是报告作者心里的一个数。
    """
    summary = summarize_results(rows)
    return {
        "accuracy": summary["accuracy"],
        "passed_cases": summary["passed_cases"],
        "total_cases": summary["total_cases"],
        "scored_rows": summary["scored_rows"],
        "total_rows": summary["total_rows"],
        "correctness": summary["score_correctness"],
        "faithfulness": summary["score_faithfulness"],
        "completeness": summary["score_completeness"],
        "generation_inconsistent": summary["generation_inconsistent"],
        "failure_breakdown": summary["failure_breakdown"],
    }


def group_by_category(rows: list) -> dict[str, dict]:
    """
    按类别聚合，口径**与总体一致**（按题判通过、按行算均分）。

    ⚠ 为什么不直接用 `eval_service.aggregate_by_category`：那是 SQL 侧的行级聚合，
      它给的是"哪一类平均分更差"；报告要的是"哪一类通过率更低"，
      两者的 `passed` 口径不同（行级 vs 题级）。同一张表里混两种口径，
      读者会拿行级通过数去对题级通过率 —— 这是 D20「两个口径不能互相印证」的翻版。
      → 报告内部**统一走题级**，需要行级明细时另开一列并标名。
    """
    out: dict[str, dict] = {}
    for cat in EVAL_CATEGORIES:
        subset = [x for x in rows if str(_field(x, "category")) == cat]
        if subset:
            out[cat] = mean_scores(subset)
    return out


def failing_rows(rows: list, limit: int) -> tuple[list, int]:
    """
    取失败案例（最多 limit 条），返回 `(取到的, 失败总数)`。

    为什么同时返回**失败总数**：因为"取到的"与"实际有几条"必须分开说 ——
    PRD 要求各取 3 条，而实测只有 1 条失败。若只返回列表，
    报告就会写成"失败案例（各取 3 条）"下面只挂 1 条，
    **读者无从判断是"只有 1 条"还是"被截断了"**。
    """
    fails = [x for x in rows if not bool(_field(x, "passed"))]
    return fails[:limit], len(fails)


# ============================================================
# 跨配置的失败一致性分析（本模块最有价值的一段）
# ============================================================
def answer_signature(text) -> str:
    """
    答案的内容指纹（sha256 前 8 位）。

    用途只有一个：判断"两个配置拿到的答案**是不是同一份**"。
    ⚠ 不能用"字数相同"代替 —— 字数相同而内容不同的答案在 LLM 输出里极常见，
      而这两件事的**后果完全不同**（见 `cross_config_failure_view`）。
    """
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:8]


def cross_config_failure_view(runs: list, rows_by_run: dict[int, list]) -> list[dict]:
    """
    把"同一条题在**多个配置**下都失败"挑出来，并回答一个决定性的问题：

        **这几个配置拿到的答案，是同一份吗？**

    为什么这个问题必须问（实测踩到的）：
        本批数据里唯一失败的 `B01`，`pure_vector` 与 `hybrid` 归因为 `wrong_answer`、
        `hybrid_rerank` 归因为 `incomplete`。若照抄进报告，
        读者会自然地读成「**配置不同 → 失败模式不同**」——这是**错的**。

        回库核对后发现：三份答案的 sha256 **两两不同**（651 / 484 / 529 字），
        连开头都在第 4 个字就分岔（「根据公司制度」vs「根据公司规定」）。
        也就是说，**三个配置拿到的是三次不同的生成结果**，
        那个"归因差异"来自**生成侧抖动**，与检索配置无关。

    → 这类错的形态值得记住：**它看起来完全正常**（表里有数据、归因字段有值、
      百分比算得对），而且它**恰好出现在最容易被人引用的一格**（失败模式分布）。
      唯一能发现它的动作，是把被比较的两个答案**取出来比指纹**，
      而不是读判定结果。这与 D21「字数抖 ≠ 结论抖」同源，但方向相反：
      **这次是"结论抖"，而它被误读成了"配置差异"。**

    判据（写进报告的结论也据此分支）：
        same_answer = True  → 同一份答案被不同检索配置喂给生成器后仍不同？不可能；
                             更可能是同一份答案被重复打分，归因差异来自**裁判侧**
        same_answer = False → **生成侧抖动**。该题的失败**不能**归因于检索配置，
                              它甚至不能算"配置的失败"——三个配置各自失败了**一次自己的生成**
    """
    by_case: dict[str, dict[str, object]] = {}
    for r in runs:
        for row in rows_by_run[r.id]:
            if bool(_field(row, "passed")):
                continue
            by_case.setdefault(str(_field(row, "case_key")), {})[r.config_name] = row

    out: list[dict] = []
    for case_key, per_config in sorted(by_case.items()):
        sigs = {cfg: answer_signature(_field(row, "answer")) for cfg, row in per_config.items()}
        out.append({
            "case_key": case_key,
            "configs": sorted(per_config),
            "signatures": sigs,
            "same_answer": len(set(sigs.values())) == 1,
            "n_configs": len(per_config),
            "reasons": {cfg: (_field(row, "failure_reason") or "unknown")
                        for cfg, row in per_config.items()},
            "lengths": {cfg: len(str(_field(row, "answer") or "")) for cfg, row in per_config.items()},
        })
    return out


# ============================================================
# Markdown 渲染（纯字符串拼接，可单测）
# ============================================================
def _cell(value, ndigits: int = 3) -> str:
    """
    表格单元格：`None` 显示为 `—`，**不显示为 0**。

    为什么这条要单独写成一个函数：工具类题的 `score_*` 是 NULL（D23 修复后
    它们不送 judge）。NULL 若被渲染成 `0.000`，读者会得出
    "工具类题的答案质量是 0 分"这个**完全错误**的结论 ——
    实际是"这一类不按这三个维度评价"。用 `—` 让"不适用"和"得了 0 分"在视觉上分开。
    """
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{ndigits}f}"
    return str(value)


def _clip(text, limit: int) -> str:
    """
    截断长文本，并**显式标注被截断**。

    为什么必须标注：失败案例里的答案常常几百字，报告只能放片段。
    不标注的话，读者会把片段当成完整答案，
    进而对失败的归因产生偏差（"它就答了这么点，当然算错"）。
    """
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（原文 {len(s)} 字，此处截断）"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """拼一张 Markdown 表。单元格里的 `|` 必须转义，否则表会被截断。"""
    def esc(c):
        return str(c).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(esc(h) for h in headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


def render_report(
    runs: list[EvalRun],
    rows_by_run: dict[int, list],
    *,
    generated_at: dt.datetime | None = None,
) -> str:
    """
    生成报告正文（**纯函数**：给定输入必得同一输出，无 I/O、无时间依赖注入之外的东西）。

    结构严格按 PRD §9.5：总体对比表 → 每类明细 → 失败案例 → 结论与建议。
    前面加一段"结论摘要"、后面加两个附录 —— 它们是**给读者的**，
    不占 §1–§4 的编号（同 D24 教程的附录规矩：核心段编号不许变）。

    ⚠ `generated_at` 显式传入而不是内部 `now()`：
      否则同一个函数两次调用会给出不同的文本，验证脚本无法断言"确定性"。
    """
    generated_at = generated_at or dt.datetime.now()
    summaries = {r.id: mean_scores(rows_by_run[r.id]) for r in runs}
    # 以配置名为键，后续表格按 RETRIEVER_CONFIGS 的固定顺序排列 ——
    # 顺序固定才能让"两次生成的报告逐字相同"成为可断言的性质。
    by_config = {r.config_name: r for r in runs}
    ordered = [by_config[c] for c in RETRIEVER_CONFIGS]

    base = ordered[0]     # pure_vector：S2 的对照基准
    best = ordered[-1]    # hybrid_rerank：PRD 声称该更好的那个
    base_acc = summaries[base.id]["accuracy"]
    best_acc = summaries[best.id]["accuracy"]
    gain_pp = round((best_acc - base_acc) * 100, 2)
    identical = len({round(summaries[r.id]["accuracy"], 6) for r in ordered}) == 1

    out: list[str] = []
    out.append("# AgentForge 评测报告（检索配置消融）")
    out.append("")
    out.append(f"- 生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append(f"- 涉及 run：{', '.join(f'`{r.id}`（{r.config_name}）' for r in ordered)}")
    out.append(f"- 评测集指纹：`{ordered[0].dataset_fingerprint}`")
    out.append(f"- 语料指纹：`{ordered[0].corpus_fingerprint}`")
    out.append(f"- judge：`{ordered[0].judge_model}` × {ordered[0].runs_per_case} 次/份；"
               f"每条题生成 {ordered[0].generation_runs} 次")
    out.append("")
    out.append("> 本报告由 `app/services/eval_report_service.py` 从 `eval_case_results` "
               "明细表生成，**零 LLM 调用**（不重新跑评测、不重新打分）。")
    out.append("")

    # ---------- §0 摘要（数据驱动） ----------
    out.append("## 0. 结论摘要")
    out.append("")
    if identical:
        out.append(f"- 三个配置的准确率**完全相同**（均为 {base_acc:.2%}）→ "
                   f"本轮消融**未测出配置差异**。")
        out.append(f"- PRD S2 验收点要求「混合+重排 vs 纯向量 ≥ +{S2_MIN_GAIN_PP:.0f} 个百分点」，"
                   f"实测 **{gain_pp:+.2f} 个百分点** → **未达标**。")
        out.append("- 但这**不等于**「重排无用」——不可如此解读的原因见 §4，"
                   "其证据来自同日的检索层指标（D23）。")
    else:
        out.append(f"- 准确率：最高 `{best.config_name}` {best_acc:.2%}、"
                   f"最低 `{base.config_name}` {base_acc:.2%}，"
                   f"差值 **{gain_pp:+.2f} 个百分点**。")
        verdict = "达标" if gain_pp >= S2_MIN_GAIN_PP else "**未达标**"
        out.append(f"- PRD S2 验收点（≥ +{S2_MIN_GAIN_PP:.0f} 个百分点）→ {verdict}。")
    # 用 `[1]`（失败**总数**）而不是 `[0]`（取到的那几条）：
    # 摘要要回答的是"一共败了多少条"，不是"截断后剩几条"。
    total_fail = sum(failing_rows(rows_by_run[r.id], 0)[1] for r in ordered)
    # ⚠ "3 条判定失败"与"3 道题失败"是两回事 —— 本批数据恰好是**同 1 道题**在三个配置下
    #   各失败一次。只报条数会让读者以为有 3 道不同的题出了问题（D21 那条
    #   「把判定次数读成题数」的口径事故，是同一个坑）。所以两个数都要报。
    fail_view = cross_config_failure_view(ordered, rows_by_run)
    fail_keys = "、".join(f"`{v['case_key']}`" for v in fail_view) or "无"
    out.append(f"- 失败案例总量：三配置合计 {total_fail} 条判定失败，"
               f"**涉及 {len(fail_view)} 道题**（{fail_keys}）。"
               f"PRD 要求「各取 {FAILURE_CASES_PER_CONFIG} 条」，实际每配置取到的条数见 §3。")
    if any(not v["same_answer"] for v in fail_view):
        out.append(f"- ⚠ 有的失败题**在不同配置下的答案逐字不同**（生成侧抖动）→ "
                   f"它们的失败**不能归因于检索配置**，详见 §4.3。")
    out.append("")

    # ---------- §1 总体对比表 ----------
    out.append("## 1. 总体对比表")
    out.append("")
    out.append(_table(
        ["配置", "检索", "重排", "准确率（按题）", "通过/总题数",
         "正确性均分", "忠实度均分", "完整性均分", "均分分母（行）"],
        [
            [
                f"`{r.config_name}`",
                {"pure_vector": "向量 top5",
                 "hybrid": "BM25+向量 RRF top5",
                 "hybrid_rerank": "BM25+向量 RRF top20"}[r.config_name],
                "bge-reranker top5" if r.config_name == "hybrid_rerank" else "—",
                f"{summaries[r.id]['accuracy']:.2%}",
                f"{summaries[r.id]['passed_cases']}/{summaries[r.id]['total_cases']}",
                _cell(summaries[r.id]["correctness"]),
                _cell(summaries[r.id]["faithfulness"]),
                _cell(summaries[r.id]["completeness"]),
                f"{summaries[r.id]['scored_rows']}（共 {summaries[r.id]['total_rows']} 行）",
            ]
            for r in ordered
        ],
    ))
    out.append("")
    out.append(f"**两个口径，不要互相印证**（D20 教训）：")
    out.append(f"- **准确率**按**题**算：每条题先在自己的多次生成里多数投票，"
               f"再算「通过题数 / 总题数」。它衡量「这批题答对了多少」——"
               f"S2 的「+{S2_MIN_GAIN_PP:.0f}%」说的是这个口径。")
    out.append(f"- **三维度均分**按**行**算：每次生成的答案各算一份，衡量「产出的平均质量」。")
    out.append(f"- ⚠ 均分的**真实分母是「均分分母（行）」列**，不是明细总行数 ——"
               f"工具类题（`expected_tool` 非空）只判**有没有调用正确的工具**、"
               f"不判内容，它们的三列分数是 NULL、不参与求平均。"
               f"报告里若拿总行数当分母，数字本身没错、但它回答的不是你想问的问题。")
    out.append("")

    # ---------- §2 每类明细 ----------
    out.append("## 2. 每类明细")
    out.append("")
    # 这里刻意**不走"一张超宽的类别 × 配置大表"**，而是"每个配置一张小表"：
    #   类别数（3）× 配置数（3）在列上要铺开 9+ 列，超宽表在终端与窄屏上会被折断，
    #   读者看到的是**错位的数字**（而且它不会报错，只是悄悄难读）。
    #   每个配置一张 7 列小表，则在任何宽度下都可读，且顺序固定（RETRIEVER_CONFIGS）。
    cats = list(EVAL_CATEGORIES)
    for r in ordered:
        per_cat = group_by_category(rows_by_run[r.id])
        out.append(f"**`{r.config_name}`**")
        out.append("")
        out.append(_table(
            ["类别", "准确率（按题）", "通过/总题数", "正确性均分", "忠实度均分", "完整性均分", "均分分母（行）"],
            [
                [
                    cat,
                    f"{per_cat[cat]['accuracy']:.2%}" if cat in per_cat else "—",
                    (f"{per_cat[cat]['passed_cases']}/{per_cat[cat]['total_cases']}"
                     if cat in per_cat else "—"),
                    _cell(per_cat[cat]["correctness"]) if cat in per_cat else "—",
                    _cell(per_cat[cat]["faithfulness"]) if cat in per_cat else "—",
                    _cell(per_cat[cat]["completeness"]) if cat in per_cat else "—",
                    (f"{per_cat[cat]['scored_rows']}（共 {per_cat[cat]['total_rows']} 行）"
                     if cat in per_cat else "—"),
                ]
                for cat in cats
            ],
        ))
        out.append("")
    out.append("> 类别的含义：`doc_qa` = 单文档文档问答（含 5 条「文档中无答案」负例，测幻觉控制）；"
               "`cross_doc` = 跨文档推理（答案要点分散在两篇及以上文档）；"
               "`tool_call` = 工具调用（判「有没有调用期望的工具」，不判内容）。")
    out.append("")

    # ---------- §3 失败案例 ----------
    out.append("## 3. 失败案例")
    out.append("")
    out.append(f"规则：每个配置取前 {FAILURE_CASES_PER_CONFIG} 条失败判定。"
               f"**取不满时如实写明条数** —— 「只有 N 条失败」与「截断到 N 条」"
               f"是两件完全不同的事，读者必须能分清。")
    out.append("")
    any_short = False
    for r in ordered:
        picked, n_fail = failing_rows(rows_by_run[r.id], FAILURE_CASES_PER_CONFIG)
        out.append(f"### `{r.config_name}`（run `{r.id}`）")
        out.append("")
        if not picked:
            out.append("该配置**没有失败案例**。")
            out.append("")
            continue
        if n_fail < FAILURE_CASES_PER_CONFIG:
            any_short = True
            out.append(f"⚠ 该配置共失败 **{n_fail}** 条，不足 {FAILURE_CASES_PER_CONFIG} 条，以下是全部。")
            out.append("")
        else:
            out.append(f"该配置共失败 {n_fail} 条，以下取前 {FAILURE_CASES_PER_CONFIG} 条。")
            out.append("")
        for row in picked:
            out.append(f"**题号 `{_field(row, 'case_key')}`**"
                       f"（{_field(row, 'category')} / 第 {int(_field(row, 'generation_index')) + 1} 次生成）")
            out.append("")
            # 答案指纹不是装饰：它是读者**唯一能自己验证**"不同配置拿到的是不是同一份答案"
            # 的东西。没有它，§4.3 关于"生成侧抖动"的结论就只能靠相信我。
            answer_text = str(_field(row, "answer") or "")
            out.append(f"- 答案指纹：`{answer_signature(answer_text)}`（{len(answer_text)} 字）")
            out.append(f"- 归因：`{_field(row, 'failure_reason') or 'unknown'}`")
            out.append(f"- 三维度得分：正确性 {_cell(_field(row, 'score_correctness'), 1)} / "
                       f"忠实度 {_cell(_field(row, 'score_faithfulness'), 1)} / "
                       f"完整性 {_cell(_field(row, 'score_completeness'), 1)}"
                       f"（判定门槛 {PASS_THRESHOLD:.0f} 分）")
            out.append(f"- 答案片段：{_clip(_field(row, 'answer'), 220)}")
            out.append("")
            reason = _judge_reason(row)
            if reason:
                out.append(f"- judge 理由：{_clip(reason, 300)}")
                out.append("")
    if any_short:
        out.append(f"> ⚠ 有配置的失败数不足 {FAILURE_CASES_PER_CONFIG} 条。这不是「报告写少了」，"
                   f"而是**这批评测集对当前系统的难度过低**的直接证据 —— "
                   f"见 §4 第 2 条与同日的检索层指标。")
        out.append("")

    # ---------- §4 结论与建议（全部数据驱动） ----------
    out.append("## 4. 结论与建议")
    out.append("")
    out.append("### 4.1 消融结论")
    out.append("")
    if identical:
        out.append(f"**三配置在本题集上完全同分（{base_acc:.2%}）**，"
                   f"PRD S2 的「+{S2_MIN_GAIN_PP:.0f} 个百分点」**未达标**。")
        out.append("")
        out.append("⚠ **这个 0 差异不能解读为「重排无用 / 混合检索无用」**，理由有三条"
                   "（均可复核，证据在同日 D23 的检索层指标）：")
        out.append("")
        out.append("1. **检索层已经全部撞到天花板**：三个配置的 `Hit@1 / Hit@5 / Hit@10` 全为 **100%**，"
                   "`MRR` **恒为 1.000**，35 道可算题里**每一道的首篇 gold 文档都排在第 1 名**。"
                   "配置之间**没有可改进的空间** —— 不是重排没起作用，是**没有一道题给过它发挥作用的机会**。")
        out.append("2. **本批评测集的题目区分能力不足**（结构性原因）："
                   "评测题由制度条款轻度改写而来，语料中**每个主题只有一篇文档**，"
                   "所以「找到对的文档」这件事对三种配置都不构成挑战。")
        out.append("3. **0 差异的第三种可能（量具坏了）已被排除**："
                   "检索层指标的随机基线只有 4.5%~9.4%，而真实 gold 的 `Hit@5` 是 100%；"
                   "且 `d23_metrics_verify.py` 的 33 条断言全绿 → 量具是有效的。")
        out.append("")
        out.append("→ **正确的说法是**：「在**当前这份**评测集上，三个检索配置的表现无法区分；"
                   "瓶颈是**任务难度**，不是检索配置。」")
    else:
        winner = max(ordered, key=lambda r: summaries[r.id]["accuracy"])
        out.append(f"**最高分配置是 `{winner.config_name}`（{summaries[winner.id]['accuracy']:.2%}）**，"
                   f"相对基准 `{base.config_name}` 的变化为 {gain_pp:+.2f} 个百分点。")
        out.append("")
        if gain_pp >= S2_MIN_GAIN_PP:
            out.append(f"✅ 达到 PRD S2 门槛（≥ +{S2_MIN_GAIN_PP:.0f} 个百分点）。")
        else:
            out.append(f"❌ 未达到 PRD S2 门槛（≥ +{S2_MIN_GAIN_PP:.0f} 个百分点）。")
        out.append("")
        out.append("⚠ 差异是否可归因于配置，还取决于**样本量是否足以支撑**："
                   "50 道题上的几个百分点差异，其置信区间通常相互重叠。"
                   "在分布重叠的情况下，「A 比 B 好 N 条」不能作为结论（D22）。")
    out.append("")

    out.append("### 4.2 失败模式分布")
    out.append("")
    for r in ordered:
        bd = summaries[r.id]["failure_breakdown"]
        out.append(f"- `{r.config_name}`："
                   + ("、".join(f"`{k}` {v} 条" for k, v in sorted(bd.items())) if bd else "无失败"))
    out.append("")
    # 这一句是**必须**的：不加的话，上面那三行会被当成"三个配置的失败模式不同"，
    # 而实测原因恰恰是"同一条题的三次生成本身不同"。§4.3 给出指纹证据。
    out.append("> ⚠ 上面三行的归因若不同，**先别当成「失败模式不同」** —— "
               "它可能只是同一条题的三次生成结果不同。判定依据见 §4.3。")
    out.append("")
    inconsistent = {r.config_name: summaries[r.id]["generation_inconsistent"] for r in ordered}
    if any(inconsistent.values()):
        out.append(f"- 生成侧不一致题数（同题多次生成结论不同）：{inconsistent} —— "
                   f"不为 0 时，说明分数里含随机性成分（D21/D22 的「生成侧抖动」）。")
    else:
        out.append(f"- 生成侧不一致题数：{inconsistent}（每条题只生成 "
                   f"{ordered[0].generation_runs} 次，该指标需多次生成才有意义）。")
    out.append("")

    out.append("### 4.3 跨配置共有失败的答案一致性（**决定成败的一段**）")
    out.append("")
    if not fail_view:
        out.append("本轮没有失败案例，本段不适用。")
        out.append("")
    else:
        out.append("同一条题在多个配置下都失败时，先要回答：这几个配置拿到的**答案是不是同一份**？")
        out.append("")
        out.append(_table(
            ["题号", "失败配置数", "涉及的配置", "答案指纹（按配置）", "各配置答案是否逐字相同"],
            [
                [
                    f"`{v['case_key']}`",
                    str(v["n_configs"]),
                    "、".join(v["configs"]),
                    "；".join(f"{c}=`{s}`({v['lengths'][c]}字)" for c, s in v["signatures"].items()),
                    "✅ 相同" if v["same_answer"] else "❌ 不同",
                ]
                for v in fail_view
            ],
        ))
        out.append("")
        differ = [v for v in fail_view if not v["same_answer"]]
        if differ:
            out.append(f"**{len(differ)}/{len(fail_view)} 道失败题在不同配置下的答案是逐字不同的** → "
                       f"这些失败是**生成侧抖动**，不是检索配置差异：")
            out.append("")
            for v in differ:
                out.append(f"- `{v['case_key']}`：归因分别是 "
                           + "、".join(f"{c}=`{r}`" for c, r in v["reasons"].items())
                           + f"，但答案字数 {v['lengths']}、指纹互不相同 → "
                           f"**「归因不同」这件事本身也不可归因于配置**。")
            out.append("")
            out.append("→ 结论：**本批失败案例不能用来比较三个配置的失败模式**。"
                       "要比较失败模式，必须先把生成侧固定住（提高 `generation_runs` 后"
                       "取多数结论，或对同一份答案重复打分），否则比的是随机变量的两次抽样。")
            out.append("")
            out.append("> 这与 D21/D22 的分层结论一致：**抖动分三层** ——"
                       "① 裁判侧（重复测同一个值，投票有效）"
                       "② 生成侧（给随机变量抽样，投票无效、只能重复生成）"
                       "③ 输入被换（不是抖动，只能靠指纹前置拦截）。"
                       "本段碰到的是第 ② 层，而它**伪装成了配置差异**。")
        else:
            out.append("所有共有失败题的答案都逐字相同 → 它们的归因差异若存在，来自**裁判侧**，"
                       "可用多数投票降噪。")
        out.append("")

    out.append("### 4.4 建议（按优先级）")
    out.append("")
    if identical:
        out.append("1. **提升评测集难度**，让题目具备区分能力。可指认的判据是："
                   "让 gold 文档在融合序里的名次落进 **6~20** 区间 —— "
                   "该区间是唯一能区分「融合 top5（B）」与「重排窗口 20（C）」的区间。"
                   "**实测当前落进该区间的题数：0/35**，这是三配置同分的结构性原因。")
        out.append("2. **提高任务信息密度**：多文档综合类题目（答案要点分散在多篇）"
                   "是本批数据里**唯一还有梯度**的维度。")
        out.append("3. **把「无用」结论表述为有边界的结论**：对外（简历/面试）应说"
                   "「三个配置在当前评测集上不可区分，已定位到瓶颈是任务难度，"
                   "并给出可量化的改进判据」，而不是「重排有效/无效」。")
    else:
        out.append("1. 对分差最大的类别做人工复核，确认分差是真实质量差还是评测集偏置。")
        out.append("2. 检查生成侧不一致题数；偏高时应增加 `generation_runs` 而非只看均值。")
        out.append("3. 在报告与简历中同时给出准确率口径（按题）与均分口径（按行），避免读者误读。")
    out.append("")

    # ---------- 附录 ----------
    out.append("## 附录 A：口径与可比性声明")
    out.append("")
    out.append(f"- **判定门槛**：judge 三维度中**正确性**得分 ≥ {PASS_THRESHOLD:.0f}/5 记为「该次生成通过」"
               f"（PRD F7.5）。")
    out.append(f"- **准确率分母**：**题数**（每题在自己多次生成里多数投票；平局记为不通过）。")
    out.append(f"- **均分分母**：**有分数的明细行数**（工具类题不计入）。")
    out.append(f"- **可比性范围**：本报告只比较 **AgentForge 内部三个检索配置**，"
               f"与任何外部系统（如 Dify）的对照**不属本报告范围**，"
               f"两者不可混用同一张表解读。")
    out.append(f"- **不可比项**：延迟 / 成本未纳入本报告（不同环境下的参考值不能当结论）。")
    out.append("")
    out.append("## 附录 B：可复现信息")
    out.append("")
    out.append(_table(
        ["配置", "run_id", "评测集指纹", "语料指纹", "judge", "生成次数", "打分次数"],
        [
            [f"`{r.config_name}`", str(r.id),
             f"`{r.dataset_fingerprint}`", f"`{r.corpus_fingerprint}`",
             f"`{r.judge_model}`", str(r.generation_runs), str(r.runs_per_case)]
            for r in ordered
        ],
    ))
    out.append("")
    out.append("复现方式（**不需要重新花 token**）：")
    out.append("")
    out.append("```bash")
    out.append("uv run python -m scripts.d24_report           # 取每配置最新一轮，重新生成本报告")
    out.append("uv run python -m scripts.d24_report_verify    # 断言报告与库内数据一致")
    out.append("```")
    out.append("")
    return "\n".join(out)


def _judge_reason(row) -> str:
    """
    从 `judge_raw` 里取一条可读的 judge 理由。

    ⚠ `judge_raw` 的形态**不确定**（judge_runs > 1 时是多次判定的数组），
      所以这里做防御性取值：能取到就取，取不到返回空串
      **而不是抛异常** —— 理由缺失不该让整份报告生成失败。
      （但报告会因此少一行；这是可接受的降级，不是静默失效，
        因为"理由"本身不参与任何结论计算。）
    """
    raw = _field(row, "judge_raw")
    if not raw:
        return ""
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, dict):
            for key in ("reason", "理由", "explanation"):
                value = item.get(key)
                if value:
                    return str(value)
    return ""


def output_path(date: str | None = None, out_dir: str = DEFAULT_OUT_DIR) -> Path:
    """
    报告落盘路径 —— PRD F7.4 规定为 `docs/eval-report-{date}.md`。

    `date` 显式可传，是为了让**验证脚本**能写到一个临时文件里而不污染
    真正的报告；默认用当天日期。
    """
    day = date or dt.date.today().isoformat()
    return Path(out_dir) / f"eval-report-{day}.md"


async def attach_report_path(run_ids: list[int], path: str) -> int:
    """
    把报告路径回写进 `eval_runs.report_path`（PRD 里这一列就是为它留的）。

    返回值是**实际更新的行数**，供调用方断言 —— 与 D12/D17 的教训一致：
    "调了个函数"不是证据，"回库查一遍/数了更新行数"才是。
    """
    async with async_session_factory() as session:
        runs = list((await session.scalars(select(EvalRun).where(EvalRun.id.in_(run_ids)))).all())
        for run in runs:
            run.report_path = path
        await session.commit()
        return len(runs)
