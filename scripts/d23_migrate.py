"""
D23 迁移：把「C 类题只判工具调用」这件事写进数据库
====================================================
对应 D23 的修复：`tool_call` 类题不再送 judge 判内容，通过与否只看工具调用是否满足要求。

--------------------------------------------------------------------------
为什么需要一次"迁移"（明明没改表结构）
--------------------------------------------------------------------------
这次改动**没有加列、没有改类型**，但改了**列的语义**：

    eval_cases.reference        对 C 类题而言不是"参考答案"，是"期望行为说明"
    eval_cases.expected_tool    从"约束条件"升格为"判定路径的开关"
    eval_case_results.score_*   工具类题整组为 NULL（不是"没跑出来"）
    eval_case_results.judge_runs 工具类题恒为 0
    eval_case_results.passed    两条判定路径，工且类题不看分数

语义只写在 Python 注释里是不够的 —— 这次 bug 的根因**恰恰就是**"语义只存在于
生产者的 docstring 里，消费者不知道"。凡是"库里这列到底是什么意思"这种问题，
答案必须在**库自己身上**能查到（`\\d+ eval_cases` 就能看到）。
所以用 `COMMENT ON COLUMN` 把语义落到数据库，而不是只留在代码里。

--------------------------------------------------------------------------
两个设计要点
--------------------------------------------------------------------------
① **不写第二份注释文本**：期望值全部从 ORM 元数据读
   （`EvalCase.__table__.c.reference.comment`），脚本只负责搬运。
   如果在这里再抄一遍注释，就等于同一句话有两个出处 —— 改一处忘一处，
   正是本项目反复修的那个病。

② **幂等证据 = "本次更新 N 列"，二次跑必须是 0**
   和 d22_migrate 同一个模式：先查当前值，只有不同才 `COMMENT ON`。
   打印"更新了几列"是**唯一能证明它真的做了事**的东西 ——
   否则一个空跑（循环体里啥也没干）也会打印"迁移完成"。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d23_migrate
"""

import asyncio

import app.models  # noqa: F401 —— 导入即注册全部模型
from sqlalchemy import text

from app.core.db import engine
from app.models.eval import EvalCase, EvalCaseResult, EvalRun

# ============================================================
# 本次语义变更涉及的列
# ============================================================
# 只同步这几列，不做"全表列注释大扫除"。
# 理由（D21 的清理作用域教训）：迁移脚本的作用域必须与"这次改了什么"严格对齐。
# 顺手同步别的列 = 一次看不见的作用域外溢 —— 跑完没人知道它还动了什么。
#
# 值 = 这一列**为什么**要重写注释（写清原因，将来才敢删这个脚本）。
TARGETS: dict[tuple[str, str], str] = {
    ("eval_cases", "reference"):
        "对 C 类题它是期望行为说明、不参与判分 —— D23 的 bug 就是被当成答案用了",
    ("eval_cases", "expected_tool"):
        "D23 起它同时是判定路径的开关（非空 = 只判工具、不判内容、不调 judge）",
    ("eval_case_results", "score_correctness"):
        "工具类题不判内容 → 该列为 NULL，三维度均分的分母要排除它",
    ("eval_case_results", "score_faithfulness"):
        "同上（工具类题 → NULL）",
    ("eval_case_results", "score_completeness"):
        "同上（工具类题 → NULL）",
    ("eval_case_results", "judge_runs"):
        "工具类题不调 judge → 恒为 0",
    ("eval_case_results", "judge_raw"):
        "工具类题不调 judge → NULL",
    ("eval_case_results", "passed"):
        "D23 起有两条判定路径，且工具类题完全不看分数",
}

_MODELS = {
    "eval_cases": EvalCase,
    "eval_runs": EvalRun,
    "eval_case_results": EvalCaseResult,
}


def expected_comments() -> dict[tuple[str, str], str]:
    """
    从 ORM 元数据取期望注释（**唯一真相来源**）。

    为什么从 ORM 取而不是在这里写一份：见模块 docstring 的设计要点①。
    """
    out: dict[tuple[str, str], str] = {}
    for key in TARGETS:
        table_name, column_name = key
        model = _MODELS[table_name]
        column = model.__table__.c.get(column_name)
        if column is None:
            raise RuntimeError(
                f"ORM 里没有 {table_name}.{column_name} —— "
                "列被改名或删除了？这个脚本的 TARGETS 需要跟着改"
            )
        out[key] = column.comment or ""
    return out


def _quote_literal(value: str) -> str:
    """
    把文本包成 SQL 字面量。

    为什么手写转义而不是用绑定参数：PostgreSQL 的 `COMMENT ON` 是**工具语句**，
    不接受预处理参数。文本来自本项目自己的模型定义（不是用户输入），
    所以只要把单引号翻倍就足够安全。
    """
    return "'" + value.replace("'", "''") + "'"


async def current_comments() -> dict[tuple[str, str], str | None]:
    """从 pg_description 读库里的实际注释（独立来源，不经过 ORM）。"""
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT c.relname AS table_name, a.attname AS column_name, d.description
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum
            WHERE n.nspname = 'public'
              AND c.relname IN ('eval_cases', 'eval_runs', 'eval_case_results')
              AND a.attnum > 0 AND NOT a.attisdropped
        """))).mappings().all()
    return {(r["table_name"], r["column_name"]): r["description"] for r in rows}


async def sync() -> int:
    """
    把不同的列注释推上去。返回**本次实际更新了几列**。

    ⚠ 这个返回值就是"脚本真的做了事"的证据：
      第二次跑必须是 0。若还是 N，说明 COMMENT 没生效（或读回来的路径不对）。
    """
    expected = expected_comments()
    current = await current_comments()

    empty = [f"{t}.{c}" for (t, c), v in expected.items() if not v]
    if empty:
        raise RuntimeError(
            f"这些列的 ORM 注释是空的：{empty} —— "
            "空注释会被推成 NULL（= 把已有说明擦掉）。先在模型里写清楚再来。"
        )

    missing = [f"{t}.{c}" for (t, c) in expected if (t, c) not in current]
    if missing:
        raise RuntimeError(
            f"库里找不到这些列：{missing} —— 表结构不对，先跑 d22_migrate / d21_migrate"
        )

    changed: list[str] = []
    async with engine.begin() as conn:
        for (table_name, column_name), want in expected.items():
            have = current[(table_name, column_name)]
            if have == want:
                continue
            await conn.execute(text(
                f'COMMENT ON COLUMN {table_name}.{column_name} IS {_quote_literal(want)}'
            ))
            changed.append(f"{table_name}.{column_name}")

    print(f"  [同步] 本次更新 {len(changed)} 列注释（目标 {len(expected)} 列）")
    for name in changed:
        print(f"         · {name}")
    return len(changed)


async def verify() -> None:
    """
    回库核对：注释真的写进去了吗、写的和模型一致吗。

    为什么必须有这一步（而不是信 sync 的返回值）：
      判据是"我回库里查了一遍"而不是"我调了个函数"。
      sync 返回 8 不等于库里真的有 8 条 —— 中间任何一层写歪了都不会报错。
    """
    expected = expected_comments()
    current = await current_comments()

    mismatched = [
        f"{t}.{c}" for (t, c), want in expected.items() if current.get((t, c)) != want
    ]
    if mismatched:
        raise SystemExit(f"❌ 回库核对失败，仍不一致：{mismatched}")

    print(f"  [核对] {len(expected)} 列注释全部与 ORM 一致")
    for (table_name, column_name), want in expected.items():
        head = want.split("。")[0][:46]
        print(f"         · {table_name}.{column_name}  {head}…")


async def main() -> None:
    print("=" * 70)
    print("D23 迁移：把字段语义写进数据库（COMMENT ON COLUMN）")
    print("=" * 70)

    print(f"\n本次同步 {len(TARGETS)} 列的语义说明：")
    for (table_name, column_name), why in TARGETS.items():
        print(f"  · {table_name}.{column_name}")
        print(f"      原因：{why}")

    print()
    changed = await sync()

    print()
    await verify()

    print()
    if changed == 0:
        print("✅ 幂等：本次新增 0 列注释（说明库里已经是最新语义）")
    else:
        print(f"✅ 完成：{changed} 列注释已更新。再跑一次应为 0，可据此验证幂等。")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
