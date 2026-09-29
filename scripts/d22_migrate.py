"""
D22 迁移：建 eval_case_results 表 + 给 eval_runs 补两列
========================================================
这个脚本存在的唯一理由：**`Base.metadata.create_all` 不会给已存在的表加列**。

    create_all(checkfirst=True) 的行为是"表存在就整张跳过"——
    不补外键、不加列、不改类型，**一声不吭**。
    于是"给 eval_runs 加了 generation_runs 列"这件事在 ORM 里成立、
    在库里不成立，而应用照常启动，直到写这条记录时抛
    `UndefinedColumnError`（或者更糟：如果查询没带这一列，永远不报错）。

所以 D22 的迁移能力 = `create_all`（建新表）+ **显式 ALTER**（改旧表）。

⚠ 顺带一个必须记住的事实：`eval_case_results` 这张表 **PRD §10 有 DDL，但库里从未存在**。
  这与 D21 的 eval_cases / eval_runs 是同一类缺口（"文档认为已有、实际从未实现"），
  这是**第二次**。所以本脚本不假设任何表已经存在，一律以回读库结构为准。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d22_migrate
"""

import asyncio

import app.models  # noqa: F401 —— 导入即注册全部模型到 Base.metadata
from sqlalchemy import text

from app.core.db import Base, engine

# ============================================================
# 期望的库结构：(data_type, udt_name) 二元组
# ============================================================
# 为什么必须是二元组、不能只看 data_type：
#   PostgreSQL 里**所有数组列**的 data_type 都是 "ARRAY"，
#   只看它分不出 UUID[] 与 text[] —— 真元素类型在 udt_name（带下划线前缀）。
# 为什么期望值要写死在这里：
#   它是"独立来源"。只回读库、不写期望，等于自证 —— 库里是什么就说什么对。
#   （D21 时这套方法当场抓出我自己写错的 `category` 期望类型：
#     String(16) 映射成 varchar 而不是 text。）
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "eval_cases": {
        "id": ("bigint", "int8"),
        "case_key": ("character varying", "varchar"),
        "category": ("character varying", "varchar"),
        "question": ("text", "text"),
        "reference": ("text", "text"),
        "evidence": ("text", "text"),
        "doc_ids": ("ARRAY", "_uuid"),
        "doc_slugs": ("ARRAY", "_text"),
        "expected_tool": ("character varying", "varchar"),
        "is_negative": ("boolean", "bool"),
        "difficulty": ("character varying", "varchar"),
        "created_at": ("timestamp with time zone", "timestamptz"),
    },
    "eval_runs": {
        "id": ("bigint", "int8"),
        "config_name": ("character varying", "varchar"),
        "dataset_fingerprint": ("character varying", "varchar"),
        # ---- D22 新增的两列 ----
        "corpus_fingerprint": ("character varying", "varchar"),
        "generation_runs": ("integer", "int4"),
        "judge_model": ("character varying", "varchar"),
        "runs_per_case": ("integer", "int4"),
        "score_correctness": ("double precision", "float8"),
        "score_faithfulness": ("double precision", "float8"),
        "score_completeness": ("double precision", "float8"),
        "accuracy": ("double precision", "float8"),
        "report_path": ("character varying", "varchar"),
        "created_at": ("timestamp with time zone", "timestamptz"),
    },
    "eval_case_results": {
        "id": ("bigint", "int8"),
        "run_id": ("bigint", "int8"),
        "case_key": ("character varying", "varchar"),
        "category": ("character varying", "varchar"),
        "generation_index": ("integer", "int4"),
        "answer": ("text", "text"),
        "retrieved_chunks": ("text", "text"),
        "tool_calls": ("jsonb", "jsonb"),
        "sources_count": ("integer", "int4"),
        "score_correctness": ("double precision", "float8"),
        "score_faithfulness": ("double precision", "float8"),
        "score_completeness": ("double precision", "float8"),
        "judge_runs": ("integer", "int4"),
        "judge_raw": ("jsonb", "jsonb"),
        "passed": ("boolean", "bool"),
        "failure_reason": ("character varying", "varchar"),
        "created_at": ("timestamp with time zone", "timestamptz"),
    },
}

# 显式补列清单：(表, 列, 类型 DDL)
# 注意类型 DDL 必须与 EXPECTED_COLUMNS 里的期望一致 —— 两处不一致时，
# ALTER 会按这里的类型建列，随后 verify_schema 按那里的期望报错。这是刻意的：
# 让"我写的 DDL"与"我以为的类型"对不上时**立刻红**，而不是建完就算过。
_ALTERS: list[tuple[str, str, str]] = [
    ("eval_runs", "corpus_fingerprint", "VARCHAR(64)"),
    ("eval_runs", "generation_runs", "INTEGER"),
]

# 建表时**不设 NOT NULL 但也不给默认值**的列，如果库里意外带了 NOT NULL，
# 会让后续插入失败。这里只验"列存在且类型对"，其余约束靠 ORM 层保证。


async def create_tables() -> list[str]:
    """建缺失的表（已存在的整张跳过）。返回本次新建的表名。"""
    async with engine.connect() as conn:
        before = await _existing_tables(conn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    async with engine.connect() as conn:
        after = await _existing_tables(conn)
    return sorted(after - before)


async def _existing_tables(conn) -> set[str]:
    rows = (await conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ))).all()
    return {row.table_name for row in rows}


async def add_missing_columns() -> list[str]:
    """
    对已存在的表补缺失的列。返回本次新增的 "表.列" 清单。

    为什么先查再加、而不是直接用 `ADD COLUMN IF NOT EXISTS`：
      两者结果一样，但先查能在输出里**如实报告"这次加了几列"**——
      "迁移跑了"和"迁移真改了东西"是两回事，第二次跑应该加 0 列。
      这个数字也是幂等性的直接证据。
    """
    added: list[str] = []
    async with engine.begin() as conn:
        for table, column, ddl_type in _ALTERS:
            exists = await conn.scalar(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
            ), {"t": table, "c": column})
            if exists:
                continue
            await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN "{column}" {ddl_type}'))
            added.append(f"{table}.{column}")
    return added


async def verify_schema() -> int:
    """回读 information_schema，与 EXPECTED_COLUMNS 逐列核对。返回失败数。"""
    failures = 0
    async with engine.connect() as conn:
        for table, expected in EXPECTED_COLUMNS.items():
            rows = (await conn.execute(text(
                "SELECT column_name, data_type, udt_name "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t"
            ), {"t": table})).all()

            if not rows:
                print(f"  [FAIL] 表 {table} 不存在")
                failures += 1
                continue

            actual = {row.column_name: (row.data_type, row.udt_name) for row in rows}
            missing = set(expected) - set(actual)
            extra = set(actual) - set(expected)
            wrong = {
                name: (expected[name], actual[name])
                for name in set(expected) & set(actual)
                if expected[name] != actual[name]
            }

            if missing or extra or wrong:
                failures += 1
                print(f"  [FAIL] {table}: 缺列={sorted(missing)} 多列={sorted(extra)}")
                for name, (want, got) in wrong.items():
                    print(f"         列 {name}: 期望 {want} 实际 {got}")
            else:
                print(f"  [OK]   {table}: {len(actual)} 列，类型全部匹配")
    return failures


async def verify_metadata_match() -> int:
    """
    ORM 声明 vs 库结构，双向交叉核对。返回失败数。

    抓的是"模型改了但忘了跑迁移"—— 它是静默的：
    应用照常启动，只是新字段永远读出来是 None（列不存在时是写库就炸，
    而"多出列"这种情况则完全不报错）。
    """
    failures = 0
    async with engine.connect() as conn:
        for table, model in Base.metadata.tables.items():
            if table not in EXPECTED_COLUMNS:
                continue
            declared = {column.name for column in model.columns}
            rows = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t"
            ), {"t": table})).all()
            actual = {row.column_name for row in rows}
            if declared != actual:
                failures += 1
                print(
                    f"  [FAIL] {table}: ORM 多出 {sorted(declared - actual)}，"
                    f"库里多出 {sorted(actual - declared)}"
                )
            else:
                print(f"  [OK]   {table}: ORM 声明与库结构一致（{len(declared)} 列）")
    return failures


async def verify_foreign_key() -> int:
    """
    核对 eval_case_results.run_id 的外键 + CASCADE。

    为什么单独验外键：**表结构对、外键没建**是最难发现的一种残缺 ——
    删除 run 时明细不会被带走，于是库里攒下一堆 `run_id` 指向不存在运行的明细。
    它们不影响任何查询（没人会去 join），但会让"库里有多少行明细"这个统计悄悄偏大。
    """
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT rc.delete_rule, kcu.column_name, ccu.table_name AS foreign_table
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = rc.constraint_name
            WHERE kcu.table_name = 'eval_case_results' AND kcu.column_name = 'run_id'
        """))).all()

    if not rows:
        print("  [FAIL] eval_case_results.run_id 没有外键约束")
        return 1
    rule = rows[0].delete_rule
    if rule != "CASCADE":
        print(f"  [FAIL] run_id 外键的 ON DELETE 是 {rule}，期望 CASCADE")
        return 1
    print(f"  [OK]   eval_case_results.run_id → {rows[0].foreign_table}（ON DELETE CASCADE）")
    return 0


async def main() -> None:
    print("=" * 74)
    print("D22 迁移：eval_case_results 建表 + eval_runs 补列")
    print("=" * 74)

    print("\n[1/5] 建缺失的表")
    created = await create_tables()
    print(f"  本次新建：{created or '（无，表已存在）'}")

    print("\n[2/5] 给已存在的表补列")
    added = await add_missing_columns()
    print(f"  本次新增列：{added or '（无，列已存在）'}")

    print("\n[3/5] 回读库结构，与期望逐列核对")
    failures = await verify_schema()

    print("\n[4/5] ORM 声明 ↔ 库结构 交叉核对")
    failures += await verify_metadata_match()

    print("\n[5/5] 外键与级联核对")
    failures += await verify_foreign_key()

    await engine.dispose()

    print()
    print("=" * 74)
    if failures:
        print(f"❌ 迁移未通过：{failures} 处不一致")
        raise SystemExit(1)
    print("✅ 迁移通过：三张评测表结构与 ORM 声明完全一致")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
