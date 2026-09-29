"""
失败题审计：把一次（或几次）评测的全部失败明细摊开，逐条定性
================================================================
起因：D23 区分度诊断跑完，三配置准确率 88%/92%/90%，看起来"没有区分度"。
      深挖失败明细时发现 **15 条失败里 13 条是评测集自身的缺陷造成的假失败**。

根因：`tool_call` 类题的 `eval_cases.reference` 写的是**给评测者的元说明**
      （"应当调用 search_documents 工具完成该请求。理由：…"），
      而不是答案内容。judge 把 reference 当标准答案 → 判定"答案没按要求执行" → 给 0 分。
      而"要不要调工具"这个约束**本来已由 `expected_tool` 字段承载**
      （`failure_taxonomy.tool_call_ok` 读的就是它）→ 同一件事写了两处。

本脚本做三件事：
  ① 列出全部失败明细，按"失败配置 / 分数 / judge 是否提到工具"逐条打标签
  ② 按归因汇总（评测集 bug / 负例题 / 其它）
  ③ 按类别看受影响面（C 类题有几条被波及）

--------------------------------------------------------------------------
判据（为什么"judge 提到工具"能定性为评测集 bug）
--------------------------------------------------------------------------
  reference 里出现"工具/检索/search"字样 且 该条被打 0 分
  → judge 的判定依据是 reference 那句元说明，不是答案对错。
  反向验证：B01（cross_doc 题）失败时 judge 一次都没提工具 —— 那是**真的内容问题**。

⚠ 这个 bug 的特征是"**稳定的假失败**"：C05~C08 在三个配置下一致失败，
  看起来像一个可靠的信号（"系统在这类问题上确实不行"），实际是评测集缺陷。
  比随机噪声更难发现 —— 随机噪声至少会自己抖出来。

用法
----
    uv run python -m scripts.d23_failure_audit            # 默认审计「每个配置最新的一轮」
    uv run python -m scripts.d23_failure_audit --run 6    # 只看某个 run
"""

import argparse
import asyncio
from collections import Counter, defaultdict

import app.models  # noqa: F401
from sqlalchemy import text

from app.core.db import engine
from app.services import failure_taxonomy

# judge 的 reason 里出现这些词 → 说明它在拿 reference 那句"元说明"当判据
TOOL_WORDS = ("工具", "检索", "search")

SHORT_NAME = {"pure_vector": "A", "hybrid": "B", "hybrid_rerank": "C"}


async def fetch(conn, run_ids: list[int] | None) -> list[dict]:
    """取明细。

    `run_ids=None` 时取的是「**每个配置最新的一轮**」，**不是**"所有
    `generation_runs = 1` 的 run" —— 重跑之后后者会从 3 条变 6 条，
    把已作废的旧轮（run 4/5/6，其 tool_call 判定已声明作废）混进归因统计，
    **且不报任何错**。口径与 `d23_diagnose.fetch_runs` 保持一致。
    """
    if run_ids:
        where, params = "where r.run_id = any(:ids)", {"ids": run_ids}
    else:
        where, params = (
            "where r.run_id in (select distinct on (config_name) id from eval_runs"
            " where generation_runs = 1 order by config_name, id desc)",
            {},
        )
    rows = (
        await conn.execute(
            text(
                f"""
                select r.run_id, e.config_name, r.case_key, r.category,
                       r.passed, r.failure_reason, r.score_correctness,
                       r.score_completeness, r.score_faithfulness, r.answer,
                       r.judge_raw, c.expected_tool, c.is_negative, c.difficulty,
                       c.reference
                from eval_case_results r
                join eval_runs e on e.id = r.run_id
                join eval_cases c on c.case_key = r.case_key
                {where}
                order by r.case_key, r.run_id
                """
            ),
            params,
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def classify(row: dict) -> str:
    """单条明细的归因。

    ⚠ **工具类题要在最前面单独分路**（2026-09-29 修复后新增），但**只在它没被打过分时**：
    方案乙之后工具类题**不再送 judge** → `judge_raw` 是空 list（占位保证位置对齐）→
    "judge 把元说明当答案"这条归因**对它们不可能成立**，
    旧代码会因 `zero == 0` 一律落到「待查」，把真实的 `tool_miss` 说成"待查"。

    而**修复前的旧 run**（4/5/6）工具类题是**真的送过 judge** 的（`judge_raw` 非空），
    那些行仍要走下面的老路 —— 否则本脚本就**改写了历史**：
    当初查出「13 条假失败」的证据会全部变成"工具题其它失败"。

    判据复用 `failure_taxonomy.is_tool_only` —— 与 runner / 判定函数同一出处，
    不许在这里再写一遍 `expected_tool is not None`（同一判断两处写 = 两份真相）。
    """
    if failure_taxonomy.is_tool_only(row["expected_tool"]) and not (row["judge_raw"] or []):
        if row["failure_reason"] == failure_taxonomy.FAILURE_TOOL_MISS:
            return "工具题未调对"
        return "工具题其它失败"

    raw = row["judge_raw"] or []
    zero = sum(1 for x in raw if isinstance(x, dict) and x.get("correctness") == 0)
    tool_mentioned = sum(
        1 for x in raw if isinstance(x, dict) and any(w in str(x.get("reason", "")) for w in TOOL_WORDS)
    )
    # reference 本身就是元说明 → 直接证据
    ref_is_meta = any(w in str(row["reference"]) for w in ("应当调用", "不应当调用", "必须检索"))

    if zero and tool_mentioned and ref_is_meta:
        return "评测集bug"
    if row["is_negative"]:
        return "负例题"
    return "待查"


def main_report(rows: list[dict]) -> None:
    failed = [r for r in rows if not r["passed"]]
    print(f"明细 {len(rows)} 条，失败 {len(failed)} 条\n")

    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in failed:
        by_case[r["case_key"]].append(r)

    print("=" * 104)
    print("失败题逐个定性")
    print("=" * 104)
    print(f"{'题号':7s}{'类别':11s}{'难度':8s}{'负例':5s}{'失败配置':12s}{'分数':12s}"
          f"{'失败原因':26s}{'归因':10s}{'judge提工具':>13s}")
    for key in sorted(by_case):
        rs = by_case[key]
        cfgs = ",".join(SHORT_NAME.get(r["config_name"], r["config_name"]) for r in rs)
        scores = "/".join(f"{r['score_correctness']:g}" for r in rs)
        reasons = "/".join(sorted({str(r["failure_reason"]) for r in rs}))
        tags = {classify(r) for r in rs}
        all_raw = [x for r in rs for x in (r["judge_raw"] or []) if isinstance(x, dict)]
        tool_hits = sum(1 for x in all_raw if any(w in str(x.get("reason", "")) for w in TOOL_WORDS))

        print(f"{key:7s}{rs[0]['category']:11s}{rs[0]['difficulty']:8s}"
              f"{'是' if rs[0]['is_negative'] else '':5s}{cfgs:12s}{scores:12s}"
              f"{reasons:26s}{'/'.join(sorted(tags)):10s}{f'{tool_hits}/{len(all_raw)}':>13s}")

    print()
    print("=" * 104)
    print("按归因汇总")
    print("=" * 104)
    counter = Counter()
    for rs in by_case.values():
        tags = {classify(r) for r in rs}
        label = "评测集bug（judge 把元说明当答案）" if "评测集bug" in tags else (
            "负例题失败（该拒答却作答）" if "负例题" in tags else "其它（真内容问题）"
        )
        counter[label] += len(rs)
    for k, v in counter.most_common():
        print(f"  {k}: {v} 条")

    print()
    print("=" * 104)
    print("按类别看受影响面")
    print("=" * 104)
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "failed": 0, "bug": 0})
    for r in rows:
        d = by_cat[r["category"]]
        d["total"] += 1
        if not r["passed"]:
            d["failed"] += 1
            if classify(r) == "评测集bug":
                d["bug"] += 1
    print(f"{'类别':12s}{'明细数':>8s}{'失败':>8s}{'其中假失败':>12s}")
    for cat in sorted(by_cat):
        d = by_cat[cat]
        print(f"{cat:12s}{d['total']:>8d}{d['failed']:>8d}{d['bug']:>12d}")

    print()
    print("=" * 104)
    print("剔除假失败后的真实成绩")
    print("=" * 104)
    runs = sorted({(r["run_id"], r["config_name"]) for r in rows})
    for run_id, cfg in runs:
        rs = [r for r in rows if r["run_id"] == run_id]
        real_fail = [r for r in rs if not r["passed"] and classify(r) != "评测集bug"]
        print(f"  run {run_id} `{cfg}`：表面 {sum(1 for r in rs if r['passed'])}/{len(rs)} 通过"
              f" → 剔除假失败后 **{len(rs) - len(real_fail)}/{len(rs)}**"
              f"（真失败题：{', '.join(r['case_key'] for r in real_fail) or '无'}）")


async def main() -> None:
    parser = argparse.ArgumentParser(description="失败题审计")
    parser.add_argument("--run", type=int, default=None, help="只看某个 run_id（默认 generation_runs=1 的全部）")
    args = parser.parse_args()

    async with engine.connect() as conn:
        rows = await fetch(conn, [args.run] if args.run else None)
    await engine.dispose()

    if not rows:
        print("没有匹配的明细。先确认 run 是否跑过。")
        return
    main_report(rows)

    print()
    print("⚠ 提醒：`评测集bug` 的判据是『reference 里含「应当调用/不应当调用」等元说明』")
    print("   + 『judge 的 0 分理由提到工具』两件事同时成立。若某条只满足其一，会归到「待查」，请人工看一眼。")
    print("⚠ 提醒：`工具题未调对` 只在**修复后**的 run 上会出现（工具类题不送 judge、`judge_raw` 为空）；")
    print("   修复前的旧 run 工具类题真的打过分，仍按老路归因 —— 两类标签不要混着比。")


if __name__ == "__main__":
    asyncio.run(main())
