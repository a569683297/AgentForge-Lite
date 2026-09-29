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

from app.config import RETRIEVER_CONFIGS
from app.core.db import engine

# D22 探针实测的生成侧抖动（10 题里 1 题翻转）—— 本轮所有差异都要与它对比
GENERATION_JITTER_FLOOR = 0.10

SCORE_BUCKETS = (5, 4, 3, 2, 1)


async def fetch_runs(conn) -> list[dict]:
    """本次诊断的三条 run：**每个配置各取最新的一条**（`generation_runs = 1`）。

    为什么不是「恰好三条」——
    D23 修完尺子（工具类题不再送 judge）必须重跑一遍，表里 `generation_runs = 1`
    的 run 就从 3 条变 6 条。旧那轮（run 4/5/6）的 tool_call 判定已作废
    （**不重算、不删**，留作证据），如果这里还按「数够不够 3 条」判断，
    重跑之后脚本会直接退出。所以改成按配置取 `id` 最大的一条。
    """
    rows = (
        await conn.execute(
            text(
                """
                select distinct on (config_name)
                       id, config_name, accuracy, score_correctness,
                       score_faithfulness, score_completeness,
                       runs_per_case, generation_runs, created_at
                from eval_runs
                where generation_runs = 1
                order by config_name, id desc
                """
            )
        )
    ).mappings().all()
    return sorted((dict(r) for r in rows), key=lambda r: r["id"])


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


def outcome_tuple(row: dict | None) -> tuple:
    """一条明细在「能不能区分配置」这件事上的**可比形态** = `(判定, 分数)`。

    ⚠ 不能只看分数（2026-09-29 修复后新增）：
    工具类题**不判内容**、分数恒为 `None` → 只看分数的话，它们在三配置下
    全是 `None`、`len(set(...)) == 1`，会被**自动算成"三配置一致"** ——
    等于把 10 条题从"有没有区分度"的统计里悄悄豁免掉，**且不报任何错**。
    """
    if row is None:
        return (None, None)
    return (bool(row["passed"]), row["score_correctness"])


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
        # ⚠ **有分数的行数**（内容题）。工具类题只判工具调用、不送 judge → 三列分数是 NULL。
        #   凡是"按分数统计"的百分比都必须用它当分母，用 n_rows 会系统性压低。
        "n_scored": len(corr),
        "passed_cases": passed_cases,
        "accuracy": accuracy,
        "accuracy_stored": run["accuracy"],
        "correctness": sum(corr) / len(corr) if corr else 0.0,
        "faithfulness": sum(faith) / len(faith) if faith else 0.0,
        "completeness": sum(comp) / len(comp) if comp else 0.0,
        "perfect_rows": sum(1 for r in rows if r["score_correctness"] == 5),
        # judge 内翻只能在"真的被 judge 打过分"的行上看 → 分母同样是有分数的行数
        "flipped_rows": sum(
            1 for r in rows if r["score_correctness"] is not None and judge_internal_flip(r["judge_raw"])
        ),
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
            f"{b['perfect_rows']}/{b['n_scored']} | {b['flipped_rows']}/{b['n_scored']} |"
        )
    # 明细行数 ≠ 有分数的行数时必须说清楚，否则后面每一列百分比的分母都是隐式的
    if any(b["n_scored"] != b["n_rows"] for b in blocks):
        skipped = {b["n_rows"] - b["n_scored"] for b in blocks}
        print()
        print(f"⚠ 明细行数 ≠ **有分数的行数**：差的 {sorted(skipped)} 行是**工具类题**"
              "（只判工具调用、不送 judge）→ **后两列的分母是有分数的行数**，不是行数。")


def print_score_distribution(blocks: list[dict]) -> None:
    print()
    print("## 二、正确性分数分布（分母 = **有分数的行数**；工具类题不判内容，不进此表）")
    print()
    header = "| 分数 | " + " | ".join(f"`{b['config']}`" for b in blocks) + " |"
    print(header)
    print("|---" * (len(blocks) + 1) + "|")
    for score in SCORE_BUCKETS:
        cells = []
        for b in blocks:
            n = sum(1 for r in b["rows"] if r["score_correctness"] == score)
            pct = n / b["n_scored"] * 100 if b["n_scored"] else 0
            cells.append(f"{n} ({pct:.0f}%)")
        print(f"| {score} 分 | " + " | ".join(cells) + " |")
    # 把"没进表的那些行"也印出来 —— 否则读者会以为 5 档加起来就是全部明细
    cells = []
    for b in blocks:
        n = b["n_rows"] - b["n_scored"]
        pct = n / b["n_rows"] * 100 if b["n_rows"] else 0
        cells.append(f"{n} ({pct:.0f}%)")
    print(f"| 无分数（工具题） | " + " | ".join(cells) + " |")


def print_per_case(blocks: list[dict]) -> None:
    """逐题并排：只列三配置**不一致**的题（那些才是"有区分潜力"的题）。

    比较的是 `outcome_tuple`（判定 + 分数），不是只看分数 —— 理由见那里。
    """
    per_config: dict[str, dict[str, dict]] = {}
    for b in blocks:
        per_config[b["config"]] = {r["case_key"]: r for r in b["rows"]}

    all_keys = sorted(set().union(*(set(v) for v in per_config.values())))
    configs = [b["config"] for b in blocks]

    differing, identical = [], []
    for key in all_keys:
        tuples = [outcome_tuple(per_config[c].get(key)) for c in configs]
        (differing if len(set(tuples)) > 1 else identical).append((key, tuples))

    print()
    print("## 三、逐题并排（只列三配置**判定或分数不一致**的题）")
    print()
    if not differing:
        print(f"**没有任何一条题在三个配置之间判定或分数不同** —— 全部 {len(all_keys)} 条题都是「一致」。")
    else:
        meta = {}
        for b in blocks:
            for r in b["rows"]:
                meta[r["case_key"]] = (r["category"], r["difficulty"], r["is_negative"])
        print("| 题号 | 类别 | 难度 | 负例 | " + " | ".join(configs) + " |")
        print("|---" * (4 + len(configs)) + "|")
        for key, tuples in differing:
            cat, diff, neg = meta.get(key, ("?", "?", False))
            cells = [
                ("过" if t[0] else "败") + ("—" if t[1] is None else f"{t[1]:g}") for t in tuples
            ]
            print(f"| {key} | {cat} | {diff} | {'是' if neg else ''} | " + " | ".join(cells) + " |")
        print()
        print("> 单元格格式：`过5` = 判定通过且正确性 5 分；`败2` = 未通过且 2 分；"
              "`过—` = 通过但**没有分数**（工具类题不判内容）。")

    print()
    print(f"- 三配置**完全一致**的题：**{len(identical)}/{len(all_keys)}**")
    print(f"- 三配置**至少一个不同**的题：**{len(differing)}/{len(all_keys)}**")
    n_tool = sum(1 for r in blocks[0]["rows"] if r["score_correctness"] is None)
    if n_tool:
        print(f"  （其中 **{n_tool}** 条是工具类题：判定依据是「有没有调对工具」，不涉及分数）")


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
    perfect_pct = c["perfect_rows"] / c["n_scored"] if c["n_scored"] else 0
    print(f"**③ 天花板效应**：C 的满分判定 **{c['perfect_rows']}/{c['n_scored']}"
          f"（{perfect_pct:.0%}）**，准确率 **{c['accuracy']:.2%}**。")
    print(f"   分母是**有分数的行数**（{c['n_rows']} 条明细里 {c['n_scored']} 条有分数）——"
          "工具类题不判内容，不在其中。")


async def main() -> None:
    async with engine.connect() as conn:
        runs = await fetch_runs(conn)
        missing = [c for c in RETRIEVER_CONFIGS if c not in {r["config_name"] for r in runs}]
        if missing:
            print(f"⚠ 缺配置：{missing}（表里 `generation_runs = 1` 的 run 共 {len(runs)} 条）")
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
    # 判据复用 outcome_tuple —— **不许在这里再写一遍**（同一判断两处写 = 两份真相）
    n_identical = sum(
        1
        for key in all_keys
        if len({outcome_tuple(p.get(key)) for p in per_config}) == 1
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
        "scored_rows": {b["config"]: b["n_scored"] for b in blocks},
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
