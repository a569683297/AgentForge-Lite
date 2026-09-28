"""
D21 ε 重测：新评测集上重新校准 judge 的噪声门槛
=================================================
为什么必须重测（D20 已定下的纪律）：
    ε 取决于**样本落在阈值带（correctness ≈ 4）的比例**。D20 的 ε = 0.95% 是在
    21 份**人工构造**的样本上量的；D21 换成了 50 条真实评测评测集，
    难度配比完全不同 → **阈值带里的样本比例变了 → ε 变 → "差几条才算显著"的门槛也必须重算**。
    不重测，D23 那张 A/B/C 对比表就没有可解释的判定线。

--------------------------------------------------------------------------
本脚本做两件事（对应决定 3 的 (a) 方案）
--------------------------------------------------------------------------
① **抽检真跑**：从 50 条里**分层抽样 10 条**，真的调一次被测系统（默认配置
   hybrid_rerank）拿答案 → 这 10 份答案的分数分布就是新评测集的难度分布 → 可测 ε。
   顺带验证了一件更要紧的事：**这些题是能作答的**。若某类全军覆没，
   说明题太难或语料没写清，那是当场就该回炉的。

② **参考答案自评**：把参考答案本身当"待评答案"喂给 judge（全 50 条）。
   正确的评测集必须满足**参考答案自己全部 ≥4 分** —— 否则它不是"不够完整"
   就是"表述和题面对不上"，**所有被测系统的分数都会被压住**，而你只会看到
   "三配置都差"，查不出原因在哪。

⚠ ②不能用来测 ε：参考答案大概率全判 5 分 → 又变成 D20 第 1 轮那个
  "阈值带没样本 → ε = 0 假数"的坑。它只做质量自检，ε 由 ① 提供。

--------------------------------------------------------------------------
副作用与清理
--------------------------------------------------------------------------
`run_agent` 会往 sessions / messages / Redis 窗口里写数据 —— 那是**对话**数据，
评测不该污染它。所以每条用一个全新 session（同时保证没有历史上下文干扰），
跑完统一清理。不清理的第二个后果更隐蔽：**重跑时读到上一轮历史** →
答案不一致 → 测出来的"抖动"里混进了历史的影响，而那不是 judge 的抖动。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_epsilon_recheck
"""

import asyncio
import os
import statistics
import uuid
from collections import Counter

import httpx
from sqlalchemy import select, text

from app.core.db import async_session_factory, engine
from app.models.eval import EvalCase
from app.services.agent_service import run_agent
from app.services.memory_service import clear_window
from scripts.d20_judge_probe import (
    CONCURRENCY,
    REQUEST_TIMEOUT,
    build_judges,
    judge_once,
    majority,
    wilson_interval,
)

# ---- 本次的重复次数与判定阈值（与 D20 一致，便于横向对比）----
RUNS = 5                    # 每个样本重复打分几次（5 次才够估比例）
PASS_THRESHOLD = 4          # correctness ≥ 4 记"对"（PRD F7.5）
EVAL_SET_SIZE = 50          # 与评测集规模一致，用于把 ε 换算成"条数门槛"
VOTE_RUNS = 3               # D20 定下的"3 次多数投票"

# ---- 两个口径的判据 ----
# 观测到的不一致次数少于这个数时，点估计（口径 A）不可信：
# "0 次不一致"只能说明 ε 小，推不出 ε = 0 —— 拿 0 当门槛会得出"任何差异都显著"。
MIN_MISMATCH_FOR_POINT_ESTIMATE = 5

# 满分判定占比超过这个比例 → 评测集区分度预警（D20 §4 预警过的"三配置都答对"风险）
DISCRIMINATION_ALERT_SHARE = 0.7

# PRD §13.2 的验收线："混合+重排 vs 纯向量 ≥ +10%"
# 50 条上 80% → 88% 就是 4 条题 —— 把门槛和这个数直接比，才能回答"测不测得出"
PRD_IMPROVEMENT_CASES = 4

# ---- 分层抽样计划：(类别, 难度, 是否负例, 抽几条) ----
# 为什么不随机抽：随机抽 10 条很可能全落在 doc_qa/easy（它占比最大），
# 于是既测不到跨文档题、也测不到边界题 —— 而"决定成败的正是 3~4 分那一档"。
SAMPLING_PLAN: list[tuple[str, str, bool, int]] = [
    ("doc_qa", "easy", False, 2),
    ("doc_qa", "medium", False, 2),
    ("doc_qa", "hard", False, 1),
    ("doc_qa", "medium", True, 1),      # 负例：测"拒答"这一档
    ("cross_doc", "medium", False, 1),
    ("cross_doc", "hard", False, 1),
    ("tool_call", "easy", False, 2),
]


def new_stats() -> dict:
    """judge_once 需要的统计容器（字段与 d20_judge_probe 对齐）。"""
    return {"request": [], "e2e": [], "errors": [], "retries": 0, "retry_recovered": 0}


def noise_floor_from_epsilon(epsilon: float, size: int = EVAL_SET_SIZE) -> tuple[float, float]:
    """
    由"单次判定不一致率"推出「两个配置各 size 条时，纯噪声期望制造出几条差异」及其门槛。

    推导：每条题在两个配置下各有 ε 概率判错、方向独立
          → 单条出现差异的概率 p = 2ε(1−ε)
          → size 条上的期望差异 = size·p，标准差 σ = √(size·p·(1−p))
          → 门槛 = 地板 + 2σ（超过它才值得怀疑"不是噪声"）
    """
    p = 2 * epsilon * (1 - epsilon)
    floor = size * p
    sigma = (size * p * (1 - p)) ** 0.5
    return floor, floor + 2 * sigma


def gate_after_voting(epsilon: float, runs: int = 3, size: int = EVAL_SET_SIZE) -> float:
    """
    runs 次多数投票后的门槛。

    "判错"从一个事件变成两个事件：单次判错概率 ε → **超过半数次判错**的概率。
    对 runs=3 就是 3ε²(1−ε)+ε³（≥2 次错）。这里用组合数写成通用形式，
    以便将来改成 5 次投票时不用改公式。
    """
    from math import comb

    p_single = sum(
        comb(runs, k) * epsilon**k * (1 - epsilon) ** (runs - k)
        for k in range(runs // 2 + 1, runs + 1)
    )
    return noise_floor_from_epsilon(p_single, size)[1]


# ============================================================
# 抽样
# ============================================================
async def sample_cases() -> list[EvalCase]:
    """按分层计划抽样（桶内按 case_key 排序取前 N 条 → 结果确定、可复现）。"""
    async with async_session_factory() as session:
        rows = list((await session.scalars(select(EvalCase).order_by(EvalCase.case_key))).all())

    picked: list[EvalCase] = []
    for category, difficulty, negative, count in SAMPLING_PLAN:
        bucket = [r for r in rows
                  if r.category == category and r.difficulty == difficulty
                  and bool(r.is_negative) == negative]
        if len(bucket) < count:
            raise RuntimeError(
                f"抽样桶 {category}/{difficulty}/neg={negative} 只有 {len(bucket)} 条，"
                f"不足 {count} 条 —— 分层抽样计划与评测集分布不匹配"
            )
        picked.extend(bucket[:count])
    return picked


# ============================================================
# ① 抽检真跑
# ============================================================
async def run_system(cases: list[EvalCase]) -> tuple[list[dict], list[uuid.UUID]]:
    """
    对抽中的题真的跑一遍被测系统（默认配置），返回 judge 输入样本与用过的 session 列表。

    每条一个**全新 session**：既避免污染对话数据，也保证没有历史上下文干扰 ——
    否则重跑时读到上一轮历史，答案会变，测出来的就不是 judge 的抖动了。
    """
    samples: list[dict] = []
    session_ids: list[uuid.UUID] = []

    for case in cases:
        session_id = uuid.uuid4()
        session_ids.append(session_id)
        result = await run_agent(session_id, case.question)

        # 检索片段：judge 的 faithfulness 维度要拿它当对照物（不是拿参考答案）
        chunks = "\n\n".join(
            f"[{item.index}] 来源：{item.source}\n{item.content}" for item in result.sources
        ) or "（本轮没有检索到任何片段）"

        retriever = "未检索"
        if result.sources:
            retriever = "hybrid_rerank（工具层默认）"

        print(f"  {case.case_key:5s} {case.category:11s} 答案 {len(result.answer):4d} 字  "
              f"片段 {len(result.sources)} 条  引用 {result.invalid_citations or '正常'}")
        samples.append({
            "id": case.case_key,
            "question": case.question,
            "reference": case.reference,
            "chunks": chunks,
            "answer": result.answer,
            "_category": case.category,
            "_is_negative": case.is_negative,
            "_retriever": retriever,
        })
    return samples, session_ids


async def cleanup_sessions(session_ids: list[uuid.UUID]) -> None:
    """清掉本次评测产生的会话数据（Redis 窗口 + PG 的 messages/sessions）。"""
    for session_id in session_ids:
        await clear_window(session_id)
    async with async_session_factory() as session:
        for session_id in session_ids:
            # 先删 messages 再删 sessions：反了会撞外键（如果外键没配 CASCADE）
            await session.execute(text("DELETE FROM messages WHERE session_id = :sid"),
                                  {"sid": session_id})
            await session.execute(text("DELETE FROM sessions WHERE id = :sid"),
                                  {"sid": session_id})
        await session.commit()
    print(f"  已清理本次评测产生的 {len(session_ids)} 个会话（Redis 窗口 + PG 记录）")


# ============================================================
# 打分
# ============================================================
async def score_samples(provider: dict, samples: list[dict], runs: int, label: str) -> list[list[dict]]:
    """对每个样本重复打 runs 次分，返回「样本 → 各次评分结果」的二维列表。"""
    stats = new_stats()
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        tasks = [
            judge_once(client, provider, sample, sem, stats)
            for sample in samples
            for _ in range(runs)
        ]
        settled = await asyncio.gather(*tasks)

    grouped: list[list[dict]] = [[] for _ in samples]
    for index, result in enumerate(settled):
        grouped[index // runs].append(result)

    ok = sum(1 for r in settled if r.get("ok"))
    print(f"  {label}：调用 {len(settled)} 次，成功 {ok}，重试 {stats['retries']} 次，"
          f"重试后恢复 {stats['retry_recovered']} 次")
    if stats["request"]:
        lat = sorted(stats["request"])
        print(f"  {label}：单次 HTTP 往返 中位 {statistics.median(lat):.2f}s "
              f"最慢 {lat[-1]:.2f}s（e2e 中位 {statistics.median(stats['e2e']):.2f}s）")
    if stats["errors"]:
        print(f"  {label}：失败样例（前 2 条）：")
        for err in stats["errors"][:2]:
            print(f"    - {err[:110]}")
    return grouped


def report_epsilon(samples: list[dict], grouped: list[list[dict]], title: str) -> float:
    """把重复打分结果换算成 ε、阈值带占比与噪声门槛。"""
    print()
    print("-" * 74)
    print(title)
    print("-" * 74)

    hist: Counter[int] = Counter()
    band = 0
    scored_total = 0
    all_runs = 0
    mismatch = 0
    flipped = 0
    scored_samples = 0

    for sample, results in zip(samples, grouped, strict=True):
        good = [r for r in results if r.get("ok")]
        if not good:
            print(f"  ⚠ {sample['id']} 全部打分失败，跳过")
            continue
        scored_samples += 1
        scores = [r["correctness"] for r in good]
        hist.update(scores)
        scored_total += len(scores)
        band += sum(1 for s in scores if abs(s - 4) < 0.5)     # 阈值带 = 恰好 4 分

        binary = [1 if s >= PASS_THRESHOLD else 0 for s in scores]
        truth = majority(binary)
        if len(set(binary)) > 1:
            flipped += 1
        all_runs += len(binary)
        mismatch += sum(1 for b in binary if b != truth)

        print(f"  {sample['id']:5s} {sample['_category']:11s} "
              f"corr={scores}  → 判定 {'对' if truth else '错'}")

    # ---- 分数直方图（先看档位分布，再看 ε —— 顺序不能反，见 D20 第 1 轮的教训）----
    print()
    print("  correctness 分档分布：")
    for score in range(6):
        count = hist.get(score, 0)
        bar = "█" * count
        mark = "  ← 阈值带" if score == 4 else ""
        print(f"    {score} | {bar} {count}{mark}")

    band_ratio = band / scored_total if scored_total else 0.0
    # 阈值带里有没有样本，是 ε 能不能信的前置判据 —— 必须先看它，再看 ε。
    # （D20 第 1 轮的教训：顺序反了，先算 ε 才发现阈值带是空的）
    if band == 0:
        print("  ⚠ 阈值带（correctness = 4）里**一条样本都没有** → "
              "翻转概率被结构性锁死为 0，")
        print("    此时算出的 ε = 0 必然是假数（D20 第 1 轮就是这样）。")
    else:
        print(f"  ✓ 阈值带里有 {band} 条判定 —— 存在翻转的可能，ε 才有意义")

    if scored_total == 0:
        print("  ⚠ 没有任何有效打分，ε 无法计算")
        return float("nan")

    epsilon = mismatch / all_runs
    low, high = wilson_interval(mismatch, all_runs)

    print()
    print(f"  样本 {scored_samples} 份 × {RUNS} 次 = {all_runs} 次判定")
    print(f"  样本级翻转（{RUNS} 次内对/错不一致）：{flipped}/{scored_samples}")
    print(f"  ε（单次与多数投票不一致的比例） = {mismatch}/{all_runs} = {epsilon:.4f} ({epsilon:.2%})")
    print(f"  ε 的 95% Wilson 区间 = {low:.4%} ~ {high:.4%}")

    # ---- 门槛必须给两个口径 ----
    # 口径 A 用点估计；口径 B 用 Wilson **上限**当 ε（最保守）。
    # 为什么非要两个：「观测 0 次不一致」既可以说"judge 完美稳定"（门槛 0，荒谬），
    # 也可以说"样本量不够，真实 ε 可能高达 7%"（门槛 11 条）。后者才是诚实的读法。
    floor_pt, gate_pt = noise_floor_from_epsilon(epsilon)
    floor_cs, gate_cs = noise_floor_from_epsilon(high)
    gate_vote_pt = gate_after_voting(epsilon, VOTE_RUNS)
    gate_vote_cs = gate_after_voting(high, VOTE_RUNS)

    print()
    print(f"  口径 A（点估计）ε={epsilon:.2%} → 地板 {floor_pt:.2f} 条 / "
          f"单次门槛 {gate_pt:.2f} 条 / {VOTE_RUNS} 次投票门槛 {gate_vote_pt:.2f} 条")
    print(f"  口径 B（保守）  ε={high:.2%}（Wilson 上限）→ 地板 {floor_cs:.2f} 条 / "
          f"单次门槛 {gate_cs:.2f} 条 / {VOTE_RUNS} 次投票门槛 {gate_vote_cs:.2f} 条")

    if mismatch < MIN_MISMATCH_FOR_POINT_ESTIMATE:
        print()
        print(f"  ⚠ 不一致只观测到 {mismatch} 次（< {MIN_MISMATCH_FOR_POINT_ESTIMATE} 次）"
              f"→ **口径 A 不可用**：")
        print("    「0 次不一致」只能说明 ε 很小，推不出 ε = 0；拿 0 当门槛会得出")
        print("    「任何差异都显著」的荒谬结论。此时只能看口径 B。")

    # ---- 区分度预警：抽检样本是不是"太简单"了 ----
    print()
    top_count = hist.get(5, 0)
    low_count = sum(hist.get(s, 0) for s in range(0, PASS_THRESHOLD))
    print(f"  分档集中度：满分(5) {top_count}/{scored_total} = {top_count / scored_total:.1%} ｜ "
          f"阈值带(4) {band}/{scored_total} = {band_ratio:.1%} ｜ "
          f"不达标(≤3) {low_count}/{scored_total} = {low_count / scored_total:.1%}")
    if top_count / scored_total >= DISCRIMINATION_ALERT_SHARE:
        print(f"  ⚠ **区分度预警**：{top_count}/{scored_total} 次判定都是满分 →")
        print("    三配置很可能都答对 → D23 的消融实验会**测不出 A/B/C 的差异**。")
        print("    注意这**不是**「重排没用」，是**题目不够难、没有区分度** ——")
        print("    D20 §4 已经预警过这个风险（全对的题区分度为 0，不提供任何信息）。")

    # ---- 直接回答 PRD 的验收线能不能判定 ----
    print()
    print(f"  对 PRD 验收线的含义（+10% 相对提升 ≈ {PRD_IMPROVEMENT_CASES} 条题）：")
    if PRD_IMPROVEMENT_CASES >= gate_cs:
        print(f"    ✓ {PRD_IMPROVEMENT_CASES} 条 ≥ 保守单次门槛 {gate_cs:.1f} 条 → 可判定")
    elif PRD_IMPROVEMENT_CASES >= gate_vote_cs:
        print(f"    △ {PRD_IMPROVEMENT_CASES} 条 < 保守单次门槛 {gate_cs:.1f} 条 → **单次打分判不出**；")
        print(f"      但 {VOTE_RUNS} 次投票后门槛降到 {gate_vote_cs:.1f} 条 → **投票后可判定**")
    else:
        print(f"    ✗ {PRD_IMPROVEMENT_CASES} 条 < 保守单次门槛 {gate_cs:.1f} 条，"
              f"且 < 保守投票门槛 {gate_vote_cs:.1f} 条")
        print(f"      → **连 {VOTE_RUNS} 次投票也判不出**。根因不是 judge，是本轮抽检")
        print("        没有造出「会翻转」的判定（样本太简单，判定离阈值太远）。")

    return epsilon


# ============================================================
# 主流程
# ============================================================
async def main() -> None:
    print("=" * 74)
    print("D21 ε 重测：新评测集上的 judge 噪声门槛")
    print("=" * 74)
    load = os.getloadavg()
    cores = os.cpu_count() or 1
    print(f"启动前 load = {load[0]:.2f} / {cores} 核")

    judge = build_judges(["deepseek"])[0]
    print(f"judge 通道 = {judge['name']} / {judge['model']}")

    print()
    print("[1/4] 分层抽样")
    cases = await sample_cases()
    print(f"  抽中 {len(cases)} 条：{', '.join(c.case_key for c in cases)}")

    print()
    print("[2/4] 真跑被测系统（默认配置，每条一个全新 session）")
    epsilon = float("nan")
    session_ids: list[uuid.UUID] = []
    try:
        samples, session_ids = await run_system(cases)

        print()
        print("[3/4] judge 重复打分")
        grouped = await score_samples(judge, samples, RUNS, "抽检真跑")
        epsilon = report_epsilon(samples, grouped, "抽检真跑的 ε")

        # ---- 参考答案自评（全部 50 条，单次；低于阈值才复测）----
        async with async_session_factory() as session:
            all_cases = list((await session.scalars(
                select(EvalCase).order_by(EvalCase.case_key)
            )).all())

        ref_samples = [
            {
                "id": c.case_key,
                "question": c.question,
                "reference": c.reference,
                # 参考答案自评时，把参考答案同时当"检索片段"：它的目的是查
                # "标准答案本身有没有资格当标尺"，不是查 faithfulness
                "chunks": c.reference,
                "answer": c.reference,
                "_category": c.category,
                "_is_negative": c.is_negative,
                "_retriever": "n/a",
            }
            for c in all_cases
        ]

        print()
        print(f"[4/4] 参考答案自评（全部 {len(ref_samples)} 条，单次打分）")
        ref_grouped = await score_samples(judge, ref_samples, 1, "参考答案自评")
        weak = [
            (s["id"], g[0]["correctness"])
            for s, g in zip(ref_samples, ref_grouped, strict=True)
            if g and g[0].get("ok") and g[0]["correctness"] < PASS_THRESHOLD
        ]
        print(f"  单次打分低于 {PASS_THRESHOLD} 分的参考答案：{len(weak)} 条")
        if weak:
            print(f"  → 对以下 {len(weak)} 条复测 2 次（单次抖动可能造成误报）：")
            weak_ids = {k for k, _ in weak}
            weak_samples = [s for s in ref_samples if s["id"] in weak_ids]
            retest = await score_samples(judge, weak_samples, 2, "参考答案复测")
            still_weak = []
            for sample, first, extra in zip(weak_samples,
                                            [g for s, g in zip(ref_samples, ref_grouped, strict=True)
                                             if s["id"] in weak_ids],
                                            retest, strict=True):
                votes = [r["correctness"] for r in first if r.get("ok")]
                votes += [r["correctness"] for r in extra if r.get("ok")]
                verdict = majority([1 if v >= PASS_THRESHOLD else 0 for v in votes])
                print(f"    {sample['id']:5s} 三次评分 = {votes} → {'通过' if verdict else '**仍不达标**'}")
                if not verdict:
                    still_weak.append(sample["id"])
            if still_weak:
                print(f"  ✗ 参考答案仍不达标的：{still_weak} —— **必须先修这些题**，"
                      f"否则所有被测系统的分数都会被压住")
            else:
                print("  [OK] 复测后全部达标（首次低于阈值是单次抖动）")
        else:
            print(f"  [OK] 全部 {len(ref_samples)} 条参考答案都 ≥{PASS_THRESHOLD} 分"
                  f"（评测集质量下限校验通过）")

    finally:
        print()
        print("清理评测产生的会话数据")
        await cleanup_sessions(session_ids)
        await engine.dispose()

    print()
    print("=" * 74)
    print(f"新评测集上的 ε（点估计）= {epsilon:.4f} ({epsilon:.2%})")
    print("⚠ 判定门槛请以「口径 B（保守）」为准 —— 不一致观测次数不足时，点估计不可用")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
