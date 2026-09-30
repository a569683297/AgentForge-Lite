"""
D24 评测报告生成（PRD F7.4）
=============================
把库里已有的评测明细变成一份报告文件。**零 LLM 调用** —— 不重新跑评测、不重新打分，
只读 `eval_runs` / `eval_case_results` 两张表。

    uv run python -m scripts.d24_report                 # 取每配置最新一轮，落盘
    uv run python -m scripts.d24_report --dry-run       # 只打印，不落盘、不写库
    uv run python -m scripts.d24_report --run-ids 9,10,11
    uv run python -m scripts.d24_report --out-dir /tmp  # 写到别处（验证脚本用）

--------------------------------------------------------------------------
为什么默认"每配置最新一轮"而不是"全部轮次"
--------------------------------------------------------------------------
`eval_runs` 里同时躺着**有效轮**（run 9/10/11，D23 修复后重跑）与**已作废的旧轮**
（run 4/5/6 是修复前跑的；run 1 只跑了 8 行）。若按"满足条件的全部"取数，
报告会把已作废的旧轮混进对比表 —— **不报错、不崩溃、表格看着也正常**。

D23 已经踩过同族坑：诊断脚本写 `where generation_runs = 1`，
第一轮恰好命中 3 条，重跑之后变成 6 条，把旧轮混进了统计。
→ 取数条件必须**随重跑自洽收缩**。见 `eval_report_service.load_latest_runs`。

--------------------------------------------------------------------------
`--dry-run` 与 `--out-dir` 的关系（容易混，写清楚）
--------------------------------------------------------------------------
    --dry-run       流程照跑（查库、守卫、渲染），但**不落盘、不回写 report_path**
    --out-dir DIR   落盘，但写到 DIR 而不是 docs/（验证脚本用它写临时报告）
两者可以一起用：`--dry-run` 优先，此时 `--out-dir` 无意义（会提示）。

⚠ 守卫不过时脚本**退出码非 0**（不是只打印一行警告）——
  写进 CI 或习惯性 `; echo $?` 的场合，"这批数据不可比"必须是个不可忽略的事实。
"""

import argparse
import asyncio
import sys
import time

import app.models  # noqa: F401 —— 导入即注册全部模型
from app.core.db import engine
from app.services import eval_report_service as rs


async def main() -> int:
    parser = argparse.ArgumentParser(description="生成评测报告（零 token）")
    parser.add_argument("--run-ids", default=None,
                        help="显式指定 run_id，逗号分隔（默认：每配置最新一轮）")
    parser.add_argument("--out-dir", default=rs.DEFAULT_OUT_DIR, help="报告输出目录")
    parser.add_argument("--date", default=None, help="报告文件名里的日期（默认今天）")
    parser.add_argument("--dry-run", action="store_true", help="只渲染并打印，不落盘、不回写")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="把完整报告打到标准输出（默认只打摘要）")
    args = parser.parse_args()

    started = time.perf_counter()
    print("=" * 74)
    print("D24 评测报告生成（PRD F7.4）—— 零 LLM 调用")
    print("=" * 74)

    # ---------- 1. 取数 ----------
    if args.run_ids:
        try:
            run_ids = [int(x) for x in args.run_ids.replace(" ", "").split(",") if x]
        except ValueError:
            print(f"❌ --run-ids 解析失败：{args.run_ids!r}（应为逗号分隔的整数）")
            await engine.dispose()
            return 2
        runs = await rs.load_runs_by_id(run_ids)
        print(f"显式指定 run：{run_ids}")
    else:
        runs = await rs.load_latest_runs()
        print(f"自动取每配置最新一轮：{[r.id for r in runs]}")

    print()
    print(f"{'run':>4} {'配置':<16}{'明细行':>7}{'judge':<10}{'生成×':>6}{'打分×':>6}  日期")
    rows_by_run: dict[int, list] = {}
    for r in sorted(runs, key=lambda x: x.id):
        rows = await rs.load_rows(r.id)
        rows_by_run[r.id] = rows
        print(f"{r.id:>4} {r.config_name:<16}{len(rows):>7}{str(r.judge_model):<10}"
              f"{str(r.generation_runs):>6}{str(r.runs_per_case):>6}  "
              f"{r.created_at.strftime('%Y-%m-%d %H:%M')}")

    # ---------- 2. 可比性守卫（不过就拒绝出报告） ----------
    print()
    print("可比性守卫（三条任一不过，就拒绝出报告）…")
    try:
        rs.check_comparable(runs, rows_by_run)
    except rs.ReportGuardError as e:
        print(f"  ❌ 不可比，拒绝生成：{e}")
        print("     这不是程序 bug，是这批数据不该拿来出报告。")
        await engine.dispose()
        return 3
    print("  ✅ 评测集指纹一致、语料指纹一致、题号集合相同 —— 考卷与教材都是同一份")
    print(f"     评测集指纹 {runs[0].dataset_fingerprint}")
    print(f"     语料指纹   {runs[0].corpus_fingerprint}")

    # ---------- 3. 渲染 ----------
    markdown = rs.render_report(runs, rows_by_run)
    path = rs.output_path(args.date, args.out_dir)

    print()
    print("报告自检（结构按 PRD §9.5，逐节检查标题是否存在）…")
    missing = [s for s in rs.REQUIRED_SECTIONS if s not in markdown]
    print(f"  必需章节 {len(rs.REQUIRED_SECTIONS)} 个，缺失：{missing or '无'}")
    # 行数先算成变量再用：f-string 里写 markdown.count("\n") 在 3.12 上虽然能跑
    # （PEP 701 允许内层同引号），但读起来像 bug，没必要赌读者的眼力。
    n_lines = markdown.count("\n") + 1
    print(f"  报告长度：{len(markdown)} 字符 / {n_lines} 行")
    # 这两个串是"渲染漏了"的指纹：库里的 NULL 若被拼进模板，会原样印成 None；
    # 而 config/judge 名一旦为 None，附录 B 的可复现信息就是假的 —— 必须当场可见。
    n_placeholder = markdown.count("TODO") + markdown.count("None")
    print(f"  疑似未渲染的占位符（'TODO' / 'None'，应为 0）：{n_placeholder}")

    if args.show:
        print()
        print("-" * 74)
        print(markdown)
        print("-" * 74)

    # ---------- 4. 落盘 + 回写 ----------
    if args.dry_run:
        print()
        print(f"[--dry-run] 未落盘、未回写 report_path。目标路径本应为：{path}")
        await engine.dispose()
        print(f"耗时：{time.perf_counter() - started:.2f}s")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    written = len(path.read_text(encoding="utf-8"))
    # 回读校验：写完当场确认磁盘上是这一份（铁律 8 附则五 ——「写了」必须等于「磁盘上有」）
    assert written == len(markdown), f"落盘后回读长度不一致：{written} != {len(markdown)}"
    print()
    print(f"已落盘：{path}（{written} 字符，回读校验通过）")

    updated = await rs.attach_report_path([r.id for r in runs], str(path))
    assert updated == len(runs), f"回写 report_path 的行数不对：{updated} != {len(runs)}"
    print(f"已回写 report_path：{updated} 行（run {[r.id for r in runs]}）")

    await engine.dispose()
    print(f"耗时：{time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
