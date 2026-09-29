"""
D23 开工前的区分度诊断
======================
读 A/B/C 三个配置的评测结果，回答三个问题（按重要性）：

  ① A（纯向量）到底多少分 —— **决定成败的未知**
     C（默认最全）已在天花板，位置无法再高 → "重排带来 +10%" 只能靠 A 更低来体现。
     A 若也是满分，则这道题上"重排有没有用"**根本测不出来**（不是没用，是尺子没分辨力）。

  ② 三配置的分数分布是否重叠
     D22 定型结论：分布重叠时，"A 比 B 好 N 条" 这种话根本不能下，跑得多努力都没用。

  ③ 天花板效应有多严重
     全是满分 = 题没有分辨力，报告里那个 92% 不叫结论。

--------------------------------------------------------------------------
⚠ 本轮的两个硬限制（写在结论里，不许省略）
--------------------------------------------------------------------------
  1. **generation_runs = 1**：单次生成的抖动实测 **10.0%**（D22 探针，10 题里 1 题翻转）
     → 本轮**只能**判断"分布 / 天花板 / A 有多差"，
       **不能**用来下 "A/B/C 谁更好"（那个差异要和 10% 的抖动地板比）
  2. 表里还有一条 D22 小规模实跑的 run（generation_runs=2，4 题）
     → 按 generation_runs = 1 过滤，它天然被排除

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    uv run python -m scripts.d23_diagnose
"""

import asyncio
import json
from collections import Counter

import app.models  # noqa: F401 —— 导入即注册全部模型
from sqlalchemy import text

from app.core.db import engine

# D22 探针实测的生成侧抖动（10 题里 1 题翻转）—— 本轮所有差异都要与它对比
GENERATION_JITTER_FLOOR = 0.10

SCORE_BUCKETS = (5, 4, 3, 2, 1)


async def fetch_runs(conn) -> list[dict]:
    """本次诊断的三个 run：generation_runs = 1（D22 小规模那条是 2，自动排除）。"""
    rows = (
        await conn.execute(
            text(
                """
                select id, config_name, accuracy, score_correctness,
                       score_faithfulness, score_completeness,
                       runs_per_case, generation_runs, created_at
                from eval_runs
                where generation_runs = 1
                order by id
                """
            )
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def fetch_details(conn, run_ids: list[int]) -> list[dict]:
    rows = (
        await conn.execute(
            text(
                """
                select r.run_id, r.case_key, r.category, r.generation_index,
                       r.passed, r.failure_reason, r.judge_raw,
                       r.score_correctness, r.score_faithfulness, r.score_completeness,
                       r.tool_calls, r.sources_count,
                       c.difficulty, c.is_negative, c.expected_tool
                from eval_case_results r
                join eval_cases c on c.case_key = r.case_key
                where r.run_id = any(:ids)
                order by r.run_id, r.case_key
                """
            ),
            {"ids": run_ids},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def judge_internal_flip(raw) -> bool:
    """同一条明细内，3 次打分的 correctness 是否出现过不同（裁判侧抖动现场）。"""
    if not isinstance(raw, list):
        return False
    scores = [r.get("correctness") for r in raw if isinstance(r, dict) and r.get("ok")]
    return len(set(scores)) > 1


def summarize(run: dict, rows: list[dict]) -> dict:
    """一个配置的汇总（accuracy 自己重算一遍，与库里存的对账）。"""
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case_key"], []).append(r)

    # 题级判定：generation_runs=1 时每题只有一行；通用起见按"全过才算过"口径说明
    passed_cases = 0
    for case_rows in by_case.values():
        votes = [r["passed"] for r in case_rows]
        if sum(votes) * 2 > len(votes):
            passed_cases += 1

    n_cases = len(by_case)
    n_rows = len(rows)
    accuracy = passed_cases / n_cases if n_cases else 0.0

    corr = [r["score_correctness"] for r in rows if r["score_correctness"] is not None]
    faith = [r["score_faithfulness"] for r in rows if r["score_faithfulness"] is not None]
    comp = [r["score_completeness"] for r in rows if r["score_completeness"] is not None]

    return {
        "run_id": run["id"],
        "config": run["config_name"],
        "n_cases": n_cases,
        "n_rows": n_rows,
        "passed_cases": passed_cases,
        "accuracy": accuracy,
        "accuracy_stored": run["accuracy"],
        "correctness": sum(corr) / len(corr) if corr else 0.0,
        "faithfulness": sum(faith) / len(faith) if faith else 0.0,
        "completeness": sum(comp) / len(comp) if comp else 0.0,
        "perfect_rows": sum(1 for r in rows if r["score_correctness"] == 5),
        "flipped_rows": sum(1 for r in rows if judge_internal_flip(r["judge_raw"])),
        "failures": Counter(r["failure_reason"] for r in rows if r["failure_reason"]),
        "rows": rows,
    }


def print_summary_table(blocks: list[dict]) -> None:
    print()
    print("## 一、三配置汇总")
    print()
    print("| 配置 | run | 题数 | 通过 | 准确率(重算) | 准确率(存量) | 正确性 | 忠实度 | 完整性 | 满分判定 | judge 内翻 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for b in blocks:
        print(
            f"| `{b['config']}` | {b['run_id']} | {b['n_cases']} | {b['passed_cases']} | "
            f"{b['accuracy']:.2%} | {b['accuracy_stored']:.2%} | "
            f"{b['correctness']:.2f} | {b['faithfulness']:.2f} | {b['completeness']:.2f} | "
            f"{b['perfect_rows']}/{b['n_rows']} | {b['flipped_rows']}/{b['n_rows']} |"
        )


def print_score_distribution(blocks: list[dict]) -> None:
    print()
    print("## 二、正确性分数分布（判定级，分母 = 明细行数）")
    print()
    header = "| 分数 | " + " | ".join(f"`{b['config']}`" for b in blocks) + " |"
    print(header)
    print("|---" * (len(blocks) + 1) + "|")
    for score in SCORE_BUCKETS:
        cells = []
        for b in blocks:
            n = sum(1 for r in b["rows"] if r["score_correctness"] == score)
            pct = n / b["n_rows"] * 100 if b["n_rows"] else 0
            cells.append(f"{n} ({pct:.0f}%)")
        print(f"| {score} 分 | " + " | ".join(cells) + " |")


def print_per_case(blocks: list[dict]) -> None:
    """逐题并排：只列三配置**不一致**的题（那些才是"有区分潜力"的题）。"""
    per_config: dict[str, dict[str, dict]] = {}
    for b in blocks:
        per_config[b["config"]] = {r["case_key"]: r for r in b["rows"]}

    all_keys = sorted(set().union(*(set(v) for v in per_config.values())))
    configs = [b["config"] for b in blocks]

    differing, identical = [], []
    for key in all_keys:
        scores = [per_config[c].get(key, {}).get("score_correctness") for c in configs]
        (differing if len(set(scores)) > 1 else identical).append((key, scores))

    print()
    print("## 三、逐题并排（只列三配置分数**不一致**的题）")
    print()
    if not differing:
        print("**没有任何一条题在三个配置之间分数不同** —— 全部 50 条题都是「一致」。")
    else:
        meta = {}
        for b in blocks:
            for r in b["rows"]:
                meta[r["case_key"]] = (r["category"], r["difficulty"], r["is_negative"])
        print("| 题号 | 类别 | 难度 | 负例 | " + " | ".join(configs) + " |")
        print("|---" * (4 + len(configs)) + "|")
        for key, scores in differing:
            cat, diff, neg = meta.get(key, ("?", "?", False))
            cells = [("—" if s is None else f"{s:g}") for s in scores]
            print(f"| {key} | {cat} | {diff} | {'是' if neg else ''} | " + " | ".join(cells) + " |")

    print()
    print(f"- 三配置**完全一致**的题：**{len(identical)}/{len(all_keys)}**")
    print(f"- 三配置**至少一个不同**的题：**{len(differing)}/{len(all_keys)}**")


def print_verdict(blocks: list[dict], n_identical: int, n_total: int) -> None:
    by_cfg = {b["config"]: b for b in blocks}
    a = by_cfg.get("pure_vector")
    c = by_cfg.get("hybrid_rerank")

    print()
    print("## 四、判定")
    print()
    if a is None or c is None:
        print("⚠ 缺少 `pure_vector` 或 `hybrid_rerank` 的结果，无法给判定。")
        return

    gap = c["accuracy"] - a["accuracy"]
    print(f"**① A（纯向量）到底多少分**：`pure_vector` 准确率 **{a['accuracy']:.2%}**"
          f"（{a['passed_cases']}/{a['n_cases']} 题），正确性均分 **{a['correctness']:.2f}**。")
    print(f"   - 与 C（`hybrid_rerank` {c['accuracy']:.2%}）的差：**{gap:+.2%}**")
    print(f"   - 噪声地板（D22 探针：单次生成抖动 **{GENERATION_JITTER_FLOOR:.1%}**）"
          f"→ 这个差{'高于' if abs(gap) > GENERATION_JITTER_FLOOR else '**未高于**'}抖动地板"
          f"{'，**方向可信但数值不可信**（本轮只生成 1 次）' if abs(gap) > GENERATION_JITTER_FLOOR else '，**本轮不足以支撑任何结论**'}")
    print()
    print(f"**② 分布重叠**：三配置完全一致的题 **{n_identical}/{n_total}**"
          f"（{n_identical / n_total:.0%}）")
    if n_identical == n_total:
        print("   - ⚠ **没有任何题能区分三配置** —— 在这个题集上，"
              "A/B/C 的差异测不出来。这叫**尺子没分辨力**，不叫「三配置一样好」。")
    else:
        print(f"   - 有区分潜力的题只有 **{n_total - n_identical}** 条，"
              f"而抖动地板是 {GENERATION_JITTER_FLOOR:.0%} → "
              f"折算到 50 条约 {GENERATION_JITTER_FLOOR * n_total:.1f} 条会随机翻转，"
              f"与可区分题的**量级相当**")
    print()
    perfect_pct = c["perfect_rows"] / c["n_rows"] if c["n_rows"] else 0
    print(f"**③ 天花板效应**：C 的满分判定 **{c['perfect_rows']}/{c['n_rows']}"
          f"（{perfect_pct:.0%}）**，准确率 **{c['accuracy']:.2%}**。")


async def main() -> None:
    async with engine.connect() as conn:
        runs = await fetch_runs(conn)
        if len(runs) != 3:
            print(f"⚠ 期望 3 条 `generation_runs = 1` 的 run，实际 {len(runs)} 条：")
            for r in runs:
                print(f"   id={r['id']} config={r['config_name']}")
            print("   → 三配置没跑齐，先跑完再诊断。")
            await engine.dispose()
            return
        details = await fetch_details(conn, [r["id"] for r in runs])
    await engine.dispose()

    by_run: dict[int, list[dict]] = {}
    for d in details:
        by_run.setdefault(d["run_id"], []).append(d)

    blocks = [summarize(r, by_run.get(r["id"], [])) for r in runs]

    print("=" * 74)
    print("D23 区分度诊断（A/B/C 各一次，generation_runs=1，judge_runs=3）")
    print("=" * 74)
    for r in runs:
        print(f"  run {r['id']}: {r['config_name']:14s} created_at={r['created_at']}")

    print_summary_table(blocks)

    # 与库里存的 accuracy 对账（同一件事两处算必须一致）
    bad = [b for b in blocks if abs(b["accuracy"] - b["accuracy_stored"]) > 0.001]
    if bad:
        print()
        for b in bad:
            print(f"⚠ 对账不一致：`{b['config']}` 重算 {b['accuracy']:.4f} "
                  f"vs 存量 {b['accuracy_stored']:.4f}")
    else:
        print()
        print(f"✅ 对账通过：三个配置的准确率，重算值与 `eval_runs` 存量值一致（0.001 容差内）")

    print_score_distribution(blocks)

    configs = [b["config"] for b in blocks]
    per_config = [{r["case_key"]: r for r in b["rows"]} for b in blocks]
    all_keys = sorted(set().union(*(set(p) for p in per_config)))
    n_identical = sum(
        1
        for key in all_keys
        if len({p.get(key, {}).get("score_correctness") for p in per_config}) == 1
    )

    print()
    print("## 失败模式分布")
    print()
    for b in blocks:
        items = "、".join(f"{k}={v}" for k, v in b["failures"].items()) or "（无）"
        print(f"- `{b['config']}`：{items}")

    print_per_case(blocks)
    print_verdict(blocks, n_identical, len(all_keys))

    print()
    print("=" * 74)
    print("⚠ 本轮限制（结论里必须一起出现）")
    print(f"  generation_runs = 1 → 单次生成抖动实测 {GENERATION_JITTER_FLOOR:.1%}（D22 探针）")
    print("  本轮只能判断「分布 / 天花板 / A 有多差」，不能下「A/B/C 谁更好」")
    print("=" * 74)

    # 便于后续写报告：把关键数字落一份 json
    out = {
        "runs": [{k: str(v) for k, v in r.items()} for r in runs],
        "summary": [
            {k: v for k, v in b.items() if k not in ("rows", "failures")} for b in blocks
        ],
        "score_distribution": {
            b["config"]: {
                str(s): sum(1 for r in b["rows"] if r["score_correctness"] == s)
                for s in SCORE_BUCKETS
            }
            for b in blocks
        },
        "identical_cases": n_identical,
        "total_cases": len(all_keys),
        "generation_jitter_floor": GENERATION_JITTER_FLOOR,
        "configs": configs,
    }
    path = "/tmp/d23_diagnose.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n关键数字已落盘：{path}")


if __name__ == "__main__":
    asyncio.run(main())
