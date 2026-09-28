"""
D21 迁移：创建 eval_cases / eval_runs 两张表
==============================================
为什么今天必须建表（而不是"PRD 说是原有表，直接用就行"）：
    PRD §10 把这两张表写作「原有表（不变）……定义见 docs/PRD.md §10」，
    但建表前实测 `information_schema.tables` 只有
    documents / document_chunks / messages / sessions **四张** ——
    评测表从来没有被实现过。不查库就发现不了，而 PRD §13.2 的 D21 验收点
    是 `/api/eval/cases` ok，它直接依赖 eval_cases 存在。

为什么用 create_all 而不是手写 CREATE TABLE（与 d16_migrate 不同）：
    d16 是**改已存在的表**（加列、加索引），create_all 见到表已存在会整张跳过，
    所以那次必须写显式 DDL。今天是**新建表**，create_all 正好是它的主场 ——
    但**仍然必须回读库结构自查**，因为：
      · create_all 的 checkfirst 只按表名判断，表以任何形式存在就跳过
      · ARRAY(Uuid) / ARRAY(Text) 这类 PG 特化类型是否真的建成了 UUID[] / text[]，
        语句成功不代表类型对

幂等性：create_all(checkfirst=True) 可反复执行；已存在的表不会被改动。
运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d21_migrate
"""

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401 —— 必须 import，否则模型不会注册到 Base.metadata
from app.core.db import Base, engine
from app.models.eval import EvalCase, EvalRun

# 期望的列结构：列名 → (data_type, udt_name)
#   ⚠ PG 里数组列的 data_type 统一是 "ARRAY"，真正的元素类型在 udt_name 上，
#     且前缀下划线（UUID[] → _uuid，text[] → _text）。只看 data_type 分不出 UUID[] 和 text[]。
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "eval_cases": {
        "id": ("bigint", "int8"),
        "case_key": ("character varying", "varchar"),
        # category / difficulty / expected_tool 声明的是 String(n) → PG 里是 varchar，
        # 不是 text。第一版期望表我写成了 text，被自查当场抓出来 ——
        # 这正是"回读库结构"的价值：它比的是**库的真实样子**，不是我脑子里的样子
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
        "judge_model": ("character varying", "varchar"),
        "runs_per_case": ("integer", "int4"),
        "score_correctness": ("double precision", "float8"),
        "score_faithfulness": ("double precision", "float8"),
        "score_completeness": ("double precision", "float8"),
        "accuracy": ("double precision", "float8"),
        "report_path": ("character varying", "varchar"),
        "created_at": ("timestamp with time zone", "timestamptz"),
    },
}


async def create_tables() -> None:
    # 只建"还没建的表"，不碰已有的任何一张（checkfirst 的语义）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    print("  [OK] create_all 执行完毕（已存在的表不会被改动）")


async def verify_schema(conn) -> None:
    """
    回读**数据库的真实结构**自查。

    为什么不信"语句没报错"：CREATE TABLE IF NOT EXISTS 在表已存在时是静默跳过的，
    语句照样返回成功。只有回读 information_schema 才知道到底有什么（D12 的教训）。
    """
    for table, expected in EXPECTED_COLUMNS.items():
        rows = (await conn.execute(text("""
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t
        """), {"t": table})).all()
        if not rows:
            raise RuntimeError(f"表 {table} 不存在（create_all 没有建成）")

        found = {r.column_name: (r.data_type, r.udt_name) for r in rows}
        missing = set(expected) - set(found)
        if missing:
            raise RuntimeError(f"表 {table} 缺列：{sorted(missing)}")
        for column, want in expected.items():
            if found[column] != want:
                raise RuntimeError(
                    f"表 {table}.{column} 类型不符：期望 (data_type, udt_name)={want}，"
                    f"实际 {found[column]}"
                )
        print(f"  [OK] {table}: {len(found)} 列，类型全部匹配")

    # case_key 的唯一性必须落到数据库上（不能只靠应用层自觉）：
    # 没有这个约束，同一条题被写两遍不会有任何报错，评测集就直接多出重复项
    idx = (await conn.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'eval_cases'
    """))).all()
    unique_on_key = [
        r.indexdef for r in idx
        if "UNIQUE" in r.indexdef.upper() and "case_key" in r.indexdef
    ]
    if not unique_on_key:
        raise RuntimeError(f"eval_cases.case_key 没有唯一约束，现有索引：{[r.indexdef for r in idx]}")
    print(f"  [OK] case_key 唯一约束存在：{unique_on_key[0]}")

    # 数据库默认值实际长什么样。
    # 为什么要专门看一眼：`server_default="{}"` 用在 ARRAY 列上时，
    # 字符串是被当成"SQL 表达式"原样发出的（DEFAULT {} 在 PG 里其实是语法错误）——
    # 它到底有没有被正确处理，只有查 column_default 才知道，不能靠猜。
    defaults = (await conn.execute(text("""
        SELECT column_name, column_default FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'eval_cases'
          AND column_default IS NOT NULL
        ORDER BY column_name
    """))).all()
    print(f"  ℹ eval_cases 带库级默认值的列："
          f"{ {r.column_name: r.column_default for r in defaults} }")


async def verify_metadata_match(conn) -> None:
    """
    交叉核对：ORM 声明的列 == 库里的列。

    这条能抓到"模型改了但忘了跑迁移"这类问题 —— 它是**静默**的：
    应用照常启动，只是那个新字段永远读出来是 None。
    """
    for model in (EvalCase, EvalRun):
        declared = {c.name for c in model.__table__.columns}
        actual = {
            r.column_name for r in (await conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :t
            """), {"t": model.__tablename__})).all()
        }
        if declared != actual:
            raise RuntimeError(
                f"{model.__tablename__} ORM 与库不一致："
                f"ORM 多出 {sorted(declared - actual)}，库里多出 {sorted(actual - declared)}"
            )
        print(f"  [OK] {model.__tablename__} ORM 声明与库结构一致（{len(declared)} 列）")


async def main() -> None:
    print("=" * 74)
    print("D21 迁移：创建 eval_cases / eval_runs")
    print("=" * 74)
    print("[1/3] 建表")
    await create_tables()
    print("[2/3] 回读库结构自查")
    async with engine.begin() as conn:
        await verify_schema(conn)
    print("[3/3] ORM 声明与库结构交叉核对")
    async with engine.begin() as conn:
        await verify_metadata_match(conn)

    await engine.dispose()
    print("=" * 74)
    print("迁移完成")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
