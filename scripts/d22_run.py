"""
D22 评测执行：跑一次完整评测并落库
====================================
这是 D22 那台"机器"的入口。D23 的消融就是用它跑三次（三个配置）。

    uv run python -m scripts.d22_run --config pure_vector
    uv run python -m scripts.d22_run --config hybrid
    uv run python -m scripts.d22_run --config hybrid_rerank

--------------------------------------------------------------------------
成本先算清楚再按回车
--------------------------------------------------------------------------
    生成：50 条 × generation_runs × (5~10s)     ← 含检索+重排+LLM
    打分：50 条 × generation_runs × judge_runs × 0.84s
默认（generation_runs=1, judge_runs=3）大约：
    生成 ≈ 5~9 分钟   打分 ≈ 2 分钟   合计 ≈ 7~11 分钟 / 配置

跑三个配置就是 20~35 分钟。**先跑 --dry-run 确认指纹与题数**，
再决定要不要按下去 —— 按错了要等十分钟才知道。

--------------------------------------------------------------------------
三个参数各自对抗什么
--------------------------------------------------------------------------
    --config         消融的自变量（唯一允许在三个 run 之间不同的东西）
    --generation-runs 对抗"生成侧抖动"（默认 1；D23 用探针数据决定要不要加到 3）
    --judge-runs      对抗"裁判侧抖动"（默认 3，D20 数据支撑）
"""

import argparse
import asyncio
import json

import app.models  # noqa: F401 —— 导入即注册全部模型（否则 ORM 关系解析不到）
from app.config import RETRIEVER_CONFIGS, settings
from app.core.db import engine
from app.models.eval import EVAL_CATEGORIES
from app.services import eval_runner


async def main() -> None:
    parser = argparse.ArgumentParser(description="跑一次评测并落库")
    parser.add_argument(
        "--config",
        default=settings.retriever_config,
        choices=RETRIEVER_CONFIGS,
        help="检索配置（消融的自变量）",
    )
    parser.add_argument(
        "--generation-runs",
        type=int,
        default=1,
        help="每条题重复生成次数（对抗生成侧抖动；默认 1，D23 按探针数据决定）",
    )
    parser.add_argument(
        "--judge-runs",
        type=int,
        default=settings.judge_runs_per_case,
        help=f"每份答案重复打分次数（默认 {settings.judge_runs_per_case}，D20 数据支撑）",
    )
    parser.add_argument("--judge", default=settings.judge_model, help="打分模型通道")
    parser.add_argument("--category", default=None, choices=EVAL_CATEGORIES, help="只跑某一类（调试）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试）")
    parser.add_argument("--dry-run", action="store_true", help="只校验指纹与题数，不真跑")
    args = parser.parse_args()

    if args.limit or args.category:
        print("=" * 74)
        print("⚠ 调试模式（--limit / --category）：这一轮的准确率**不可作为消融结论**，")
        print("  分母不是完整评测集，不同配置之间也不可比。")
        print("=" * 74)

    print("=" * 74)
    print("D22 评测执行")
    print("=" * 74)

    if args.dry_run:
        from sqlalchemy import text

        from app.services import eval_service

        dataset_fp, case_count = await eval_service.fingerprint_from_db()
        corpus_fp, chunk_count = await eval_service.corpus_fingerprint_from_db()
        # 工具类题（D23 修复后）**不送 judge** —— 打分次数要按"内容题"算，
        # 否则预估会凭空多出 30 次/配置（10 题 × 3 次）。
        async with engine.connect() as conn:
            n_tool = (
                await conn.execute(
                    text("select count(*) from eval_cases where expected_tool is not null")
                )
            ).scalar_one()
        print(f"  评测集指纹 : {dataset_fp}")
        print(f"  语料指纹   : {corpus_fp}")
        print(f"  题数       : {case_count}   切片数：{chunk_count}")
        print(f"  配置       : {args.config}")
        print(f"  题目构成   : 内容题 {case_count - n_tool} 条（要打分）+ 工具类题 {n_tool} 条（不送 judge）")
        print(
            f"  预计耗时   : 生成 {case_count * args.generation_runs} 次 + "
            f"打分 {(case_count - n_tool) * args.generation_runs * args.judge_runs} 次"
        )
        await engine.dispose()
        print("\n[--dry-run] 未实际执行。")
        return

    result = await eval_runner.run_evaluation(
        args.config,
        generation_runs=args.generation_runs,
        runs_per_case=args.judge_runs,
        judge_model=args.judge,
        category=args.category,
        limit=args.limit,
    )
    await engine.dispose()

    print()
    print("=" * 74)
    print(f"run_id       : {result['run_id']}")
    print(f"配置         : {result['config_name']}")
    print(f"评测集指纹   : {result['dataset_fingerprint']}")
    print(f"语料指纹     : {result['corpus_fingerprint']}")
    print(f"judge        : {result['judge_model']} × {result['runs_per_case']} 次/份")
    print(f"生成次数     : {result['generation_runs']}")
    print(f"明细行数     : {result['rows_written']}")
    print("-" * 74)
    print(f"准确率       : {result['accuracy']:.2%}  "
          f"（{result['passed_cases']}/{result['total_cases']} 题，分母是**题数**）")
    print(f"正确性均分   : {result['score_correctness']}  "
          f"（分母是**有分数的行数** {result['scored_rows']}，与上面的口径不同）")
    print(f"忠实度均分   : {result['score_faithfulness']}")
    print(f"完整性均分   : {result['score_completeness']}")
    if result["scored_rows"] != result["total_rows"]:
        # D23 修复（方案乙）之后这里必然不等：工具类题只判工具调用、不送 judge，
        # 三列分数是 NULL，不参与求平均。差多少行 = 有多少条工具类题。
        print(f"  ⚠ 明细行数 {result['total_rows']} ≠ 有分数的行数 {result['scored_rows']}"
              f"（差的 {result['total_rows'] - result['scored_rows']} 行是**工具类题**："
              f"只判工具调用、不判内容，故三列均分里没有它们）")
    print(f"生成不一致题 : {result['generation_inconsistent']}"
          "（同题多次生成结论不同的题数 —— 这个数不低时，分数里有随机性成分）")
    print(f"失败模式分布 : {json.dumps(result['failure_breakdown'], ensure_ascii=False)}")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
