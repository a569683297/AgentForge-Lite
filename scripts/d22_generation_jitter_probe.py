"""
D22 探针：量一量"同一条题重复生成"的分数散布
==============================================
这是决策 1（**生成要不要也重复 N 次**）的**数据来源**。
D22 的代码把 `generation_runs` 参数做出来了、默认 1；这个探针负责回答
"默认值应该改成几"。

--------------------------------------------------------------------------
为什么必须实测，不能凭一条案例推广
--------------------------------------------------------------------------
D21 观测到 B01 同配置两次独立运行：459 字判**对** / 511 字判**错**，
而 judge 内部各自 5 次完全一致。这证明了"生成侧抖动存在且可能大于裁判侧"，
但**一个案例推不出分布** —— 可能就是这一条题特别飘。

如果只有少数题飘，那么 `generation_runs=3` 是花 3 倍成本买一点点方差下降；
如果大面积飘，那不重复生成的话，D23 的 A/B/C 差异会被随机性淹没。
两种情况的决策**相反**，所以必须先量。

--------------------------------------------------------------------------
方法：三层，缺一层结论就会错
--------------------------------------------------------------------------
    ① 每道题生成 N 次            → 拿到 N 份**不同的答案**（这才是生成侧抽样）
    ② 每份答案打 M 次分，取多数   → 压掉**裁判侧**噪声
    ③ 比较 N 份答案的"多数结论"   → 剩下的变化只可能来自生成

⚠ 第 ② 步**不能省**：省掉它，裁判的抖动会被误记成生成的抖动，
  **夸大生成侧**，于是你会得出"要重复生成 5 次"这种过度投入的结论。
  这与 D22 决策材料里"三层抖动"那段是同一条纪律。

⚠ 反面：也不能把 N 份答案**合在一起**打分取多数。那等于把"5 份不同答案"
  当成"同一份答案的 5 次测量"，把生成侧抖动**抹掉**成裁判侧抖动 ——
  方向相反的同一个错误。

--------------------------------------------------------------------------
对象选 cross_doc（跨文档推理）
--------------------------------------------------------------------------
理由：它需要两篇文档的信息拼起来才能答对，是最容易出现
"这次检索到了 A、下次检索到了 B"的一类。如果连它都不怎么飘，
那别类更不用担心。这是**往最坏处取样**，不是随机抽样。
（D21 的 ε 重测用分层抽样，因为那是量"整卷"；这里是量"最坏情形"，
  取样策略相反是因为问题不同。）

--------------------------------------------------------------------------
本探针**不落库**
--------------------------------------------------------------------------
它是量具校准（同 D20 的 judge 探针），不是一次评测运行 ——
写进 eval_runs 会让"这个配置的成绩"混进校准数据。

运行（必须在项目根目录，约 5~8 分钟）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d22_generation_jitter_probe
"""

import asyncio
import statistics
import time
import uuid

import app.models  # noqa: F401
from app.config import settings
from app.core.db import engine
from app.models.eval import EvalCase
from app.services import eval_service, failure_taxonomy, judge_service
from app.services.agent_service import run_agent

# ---- 探针参数 ----
GENERATION_RUNS = 5          # 每道题生成几次（5 次才够看分布，3 次只能看"有没有翻"）
JUDGE_RUNS = 3               # 每份答案打几次分（压裁判噪声 —— 不能省）
CATEGORY = "cross_doc"
CONCURRENCY = 4


def wilson_upper(successes: int, n: int, z: float = 1.96) -> float:
    """
    二项比例的 Wilson 区间**上界**（只取上界，因为我们关心"最坏能有多差"）。

    为什么不用 p̂ ± 1.96·se：观测到 0 次翻转时 se = 0，区间退化成 [0, 0] ——
    看起来"绝对确定"，实则完全没有信息。Wilson 会给出一个诚实的上界。
    （D20 的 ε 重测踩过这个：0/50 时点估计不可用，只能报上界。）
    """
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return min(1.0, center + half)


async def load_targets() -> list[EvalCase]:
    """取 cross_doc 全部题作为探针对象（理由见模块 docstring）。"""
    cases = await eval_service.list_cases(category=CATEGORY, limit=500)
    if not cases:
        raise RuntimeError("评测集里没有 cross_doc 类题 —— 先跑 scripts/d21_seed")
    return cases


async def _judge_with_semaphore(
    semaphore: asyncio.Semaphore, case: EvalCase, answer: str, chunks: str
) -> dict:
    """在并发护栏内打一次分（参数显式传，避免循环变量闭包）"""
    async with semaphore:
        return await judge_service.judge_once(
            question=case.question,
            reference=case.reference,
            chunks=chunks,
            answer=answer,
            judge_model=settings.judge_model,
        )


async def probe_case(case: EvalCase, semaphore: asyncio.Semaphore) -> dict:
    """
    对一道题做完整的三层探针，返回该题的观测结果。
    """
    per_generation: list[dict] = []

    for generation_index in range(GENERATION_RUNS):
        session_id = uuid.uuid4()
        try:
            result = await run_agent(session_id, case.question, persist=False)
            answer = result.answer
            chunks = "\n\n".join(
                f"[{item.index}] 来源：{item.source}\n{item.content}" for item in result.sources
            ) or "（本轮没有检索到任何片段）"
            tool_calls = list(result.tool_calls)
            failed = False
        except Exception as exc:  # noqa: BLE001
            answer, chunks, tool_calls, failed = "", "（生成失败）", [], True
            print(f"    {case.case_key}#{generation_index} 生成失败: {exc}")

        if failed:
            per_generation.append({
                "index": generation_index, "length": 0, "passed": False,
                "scores": None, "error": True,
            })
            continue

        # ② 每份答案打 M 次分，取多数（压裁判噪声）
        scored = await asyncio.gather(*[
            _judge_with_semaphore(semaphore, case, answer, chunks)
            for _ in range(JUDGE_RUNS)
        ])
        outcome = judge_service.aggregate(list(scored))
        if outcome.all_failed:
            raise RuntimeError(f"{case.case_key}#{generation_index} 打分全失败：{outcome.errors[:2]}")

        passed, _reason = failure_taxonomy.derive_case_outcome(
            correctness=outcome.correctness,
            faithfulness=outcome.faithfulness,
            completeness=outcome.completeness,
            expected_tool=case.expected_tool,
            actual_tools=tool_calls,
        )
        per_generation.append({
            "index": generation_index,
            "length": len(answer),
            "passed": passed,
            "scores": (outcome.correctness, outcome.faithfulness, outcome.completeness),
            # 同一份答案内部 3 次打分的二值结论 —— 用来对照裁判侧抖动
            "judge_votes": [1 if (r.get("correctness") or 0) >= 4 else 0
                            for r in outcome.raw if r.get("ok")],
            "error": False,
        })

    passes = [g["passed"] for g in per_generation if not g["error"]]
    return {
        "case_key": case.case_key,
        "difficulty": case.difficulty,
        "generations": per_generation,
        "pass_count": sum(passes),
        "total": len(passes),
        "inconsistent": 0 < sum(passes) < len(passes) if passes else False,
        "lengths": [g["length"] for g in per_generation if not g["error"]],
        # 裁判侧：把每份答案内部的多次打分也算一遍翻转（对照组）
        "judge_flips": sum(
            1 for g in per_generation
            if not g["error"] and 0 < sum(g["judge_votes"]) < len(g["judge_votes"])
        ),
    }


async def main() -> None:
    cases = await load_targets()
    total_generations = len(cases) * GENERATION_RUNS

    print("=" * 78)
    print("D22 探针：生成侧抖动（每条题重复生成，每份答案重复打分）")
    print("=" * 78)
    print(f"  配置       : {settings.retriever_config}")
    print(f"  judge      : {settings.judge_model} × {JUDGE_RUNS} 次/份")
    print(f"  生成次数   : {GENERATION_RUNS} 次/题")
    print(f"  对象       : {CATEGORY} 全部 {len(cases)} 题（往最坏处取样）")
    print(f"  总计       : {total_generations} 次生成 + {total_generations * JUDGE_RUNS} 次打分")
    print("=" * 78)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    started = time.perf_counter()
    results = []
    for case in cases:
        outcome = await probe_case(case, semaphore)
        results.append(outcome)
        marks = "".join("✓" if g["passed"] else ("✗" if not g["error"] else "!") for g in outcome["generations"])
        lens = "/".join(str(length) for length in outcome["lengths"])
        flag = "  ← 不一致" if outcome["inconsistent"] else ""
        print(f"  {outcome['case_key']:5s} {outcome['difficulty']:7s} {marks}  "
              f"通过 {outcome['pass_count']}/{outcome['total']}  字数 {lens}{flag}")

    elapsed = time.perf_counter() - started

    # ---- 汇总 ----
    usable = [r for r in results if r["total"] > 0]
    inconsistent = [r for r in usable if r["inconsistent"]]
    # "结论一致"= N 次生成要么全过、要么全不过。它是"这条题稳不稳"的正向计数。
    consistent_cases = sum(
        1 for r in usable if r["pass_count"] == 0 or r["pass_count"] == r["total"]
    )
    judge_flip_cases = [r for r in results if r["judge_flips"] > 0]

    n = len(usable)
    gen_rate = len(inconsistent) / n if n else 0.0
    gen_upper = wilson_upper(len(inconsistent), n) if n else 0.0

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"  耗时                    : {elapsed:.1f}s")
    print(f"  可用题数                : {n}")
    print(f"  **生成侧不一致题数**    : {len(inconsistent)}/{n}  = {gen_rate:.1%}")
    print(f"  生成侧不一致率（Wilson 上界）: {gen_upper:.1%}")
    print(f"  结论一致的题            : {consistent_cases}/{n}")

    if usable:
        lengths = [length for r in usable for length in r["lengths"]]
        if lengths:
            print(f"  答案字数                : 中位 {statistics.median(lengths):.0f}  "
                  f"最短 {min(lengths)}  最长 {max(lengths)}")
        spreads = [max(r["lengths"]) - min(r["lengths"]) for r in usable if r["lengths"]]
        if spreads:
            print(f"  同题字数极差            : 中位 {statistics.median(spreads):.0f}  最大 {max(spreads)}")

    # ---- 对照组：裁判侧 ----
    print()
    print(f"  对照 · 裁判侧翻转题数   : {len(judge_flip_cases)}/{n}"
          "（同一份答案内部多次打分结论不同的题）")
    print("  对照 · 历史裁判侧 ε     : 0.95%（D20 点估计） / 7.14%（保守上界）")

    # ---- 结论 ----
    print()
    print("=" * 78)
    print("对 D23 的含义")
    print("=" * 78)
    if gen_rate == 0:
        print("  ⚠ 本次**没有观测到**生成侧不一致。但「观测到 0 次」推不出「不会发生」——")
        print(f"    按 Wilson 上界，真实不一致率仍可能高达 {gen_upper:.1%}。")
        print(f"    起点保守估计：{gen_upper:.1%} × 50 条题 ≈ {gen_upper * 50:.1f} 条题可能翻转。")
    else:
        print(f"  生成侧不一致率 {gen_rate:.1%}（上界 {gen_upper:.1%}）")
        print(f"  → 50 条题规模下，约有 {gen_rate * 50:.1f} ~ {gen_upper * 50:.1f} 条题"
              "的结论会被随机性左右")
    print("  PRD 的 +10% 验收线 ≈ 4 条题。把上面这个条数与 4 比较：")
    print(f"    若生成侧不确定的条数 ≳ 4，则**单次生成不足以判定 +10%**，"
          "D23 应把 generation_runs 提到 3。")
    print("=" * 78)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
