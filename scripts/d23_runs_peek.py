"""
看一眼 eval_runs 现状（只读，不写库）
=====================================

    uv run python -m scripts.d23_runs_peek

答一个问题：**重跑之前，表里已经有哪些 run，够不够 d23_diagnose 用。**

`d23_diagnose` 按 `generation_runs = 1` 过滤，期望恰好 3 条（A/B/C 各一）。
一旦重跑一次，同一个 `generation_runs = 1` 的 run 就会变成 6 条 ——
那时诊断脚本必须改成「每个配置取最新一条」，否则它会直接退出。
这个脚本就是用来在动手之前把这件事说清楚的。
"""

import asyncio

import app.models  # noqa: F401 —— 导入即注册全部模型
from sqlalchemy import text

from app.core.db import engine


async def main() -> None:
    async with engine.connect() as conn:
        runs = (
            await conn.execute(
                text(
                    """
                    select id, config_name, generation_runs, runs_per_case,
                           accuracy, dataset_fingerprint, created_at
                    from eval_runs
                    order by id
                    """
                )
            )
        ).mappings().all()

        counts = (
            await conn.execute(
                text(
                    """
                    select run_id,
                           count(*)                               as rows,
                           count(distinct case_key)               as cases,
                           count(*) filter (where passed)         as passed
                    from eval_case_results
                    group by run_id
                    order by run_id
                    """
                )
            )
        ).mappings().all()

    await engine.dispose()
    by_run = {c["run_id"]: c for c in counts}

    print("=" * 78)
    print("eval_runs 现状")
    print("=" * 78)
    print(f"{'id':>4}  {'config':<16} {'gen':>4} {'judge':>6} {'acc':>7} "
          f"{'行':>5} {'题':>5} {'过':>5}  评测集指纹")
    for r in runs:
        c = by_run.get(r["id"], {})
        fp = (r["dataset_fingerprint"] or "")[:12]
        print(
            f"{r['id']:>4}  {r['config_name']:<16} {r['generation_runs']:>4} "
            f"{r['runs_per_case']:>6} {r['accuracy']:>7.2%} "
            f"{c.get('rows', 0):>5} {c.get('cases', 0):>5} {c.get('passed', 0):>5}  {fp}…"
        )

    print()
    print("明细行数：", "、".join(f"run {c['run_id']}={c['rows']}" for c in counts) or "（空）")

    n_gen1 = sum(1 for r in runs if r["generation_runs"] == 1)
    print()
    print(f"`generation_runs = 1` 的 run：**{n_gen1}** 条")
    if n_gen1 == 3:
        print("→ 正好 A/B/C 各一条，`d23_diagnose` 可以直接跑。")
    elif n_gen1 > 3:
        print("→ 多于 3 条（说明有重跑）。`d23_diagnose` 目前会因「期望 3 条」而退出，")
        print("  需要先把它改成「每个配置取最新一条」。")
    else:
        print("→ 不满 3 条，三配置没跑齐。")


if __name__ == "__main__":
    asyncio.run(main())
