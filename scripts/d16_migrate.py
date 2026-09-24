"""
D16 迁移：document_chunks 补分词列 + GIN 倒排索引 + 回填历史切片
=================================================================
为什么必须写成迁移脚本，而不是在模型里加两列了事：
    铁律 10 —— `Base.metadata.create_all(checkfirst=True)` 见到表已存在就**整张跳过**，
    不补列、不改类型、不建索引。D12 的 `document_chunks` 曾因此长期没有外键，
    `ON DELETE CASCADE` 从未生效，还留下了孤儿切片。
    所以这里用**显式 DDL**，做完立刻回读库结构自查，自查不过就抛异常整体回滚。

三个动作：
    ① 加列 `content_tokens(TEXT)`  —— 应用侧写入（jieba 分词结果）
    ② 加列 `content_tsv(tsvector)` —— 生成列，数据库从 ① 派生，应用侧永远不用管它
    ③ 建 GIN 倒排索引              —— 检索时用它「筛候选」

外加**回填**：历史切片的 `content_tokens` 是 NULL。不回填，这些切片永远搜不到 ——
「列加了但数据是空的」比「不加列」更危险：前者看起来一切正常。

幂等性：DDL 全部带 IF NOT EXISTS，回填只动 `content_tokens IS NULL` 的行。
所以本脚本可以反复执行；将来**换分词器时重跑本脚本**即可让全量数据按新规则重新分词。

运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d16_migrate
"""

import asyncio

from sqlalchemy import text

from app.core.db import engine
from app.services.tokenizer import tokenize_to_string

DDL_STATEMENTS: list[tuple[str, str]] = [
    (
        "加列 content_tokens(TEXT)",
        "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS content_tokens TEXT",
    ),
    (
        "加列 content_tsv(tsvector) 作为生成列",
        """
        ALTER TABLE document_chunks
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', content_tokens)) STORED
        """,
    ),
    (
        "建 GIN 倒排索引 idx_chunks_tsv",
        """
        CREATE INDEX IF NOT EXISTS idx_chunks_tsv
        ON document_chunks USING GIN (content_tsv)
        """,
    ),
]

# 自查用的期望结构：列名 → (data_type, is_generated)
EXPECTED_COLUMNS = {
    "content_tokens": ("text", "NEVER"),
    "content_tsv": ("tsvector", "ALWAYS"),
}


async def apply_ddl(conn) -> None:
    for label, ddl in DDL_STATEMENTS:
        await conn.execute(text(ddl))
        print(f"  [OK] {label}")


async def verify_schema(conn) -> None:
    """
    建完立刻回读**数据库的真实结构**自查。

    为什么不相信"语句没报错"：ALTER ... IF NOT EXISTS 在列已存在时是**静默跳过**的，
    语句照样返回成功。只有回读 information_schema 才知道到底建成了什么（D12 的教训）。
    """
    rows = (await conn.execute(text("""
        SELECT column_name, data_type, is_generated
        FROM information_schema.columns
        WHERE table_name = 'document_chunks'
          AND column_name IN ('content_tokens', 'content_tsv')
    """))).all()
    found = {r.column_name: (r.data_type, r.is_generated) for r in rows}

    for column, expected in EXPECTED_COLUMNS.items():
        actual = found.get(column)
        if actual != expected:
            raise RuntimeError(
                f"列 {column} 结构不符：期望 (data_type, is_generated)={expected}，实际 {actual}"
            )
    print(f"  [OK] 列结构自查通过：{found}")

    indexdef = (await conn.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE tablename = 'document_chunks' AND indexname = 'idx_chunks_tsv'
    """))).scalar()
    if not indexdef or "gin" not in indexdef.lower():
        raise RuntimeError(f"GIN 索引未生效，pg_indexes 里查到的是：{indexdef!r}")
    print(f"  [OK] 索引自查通过：{indexdef}")


async def backfill(conn) -> int:
    """把历史切片逐行分词后写回。UPDATE 会顺带触发数据库重算生成列 content_tsv。"""
    rows = (await conn.execute(text("""
        SELECT id, coalesce(content, '') AS content
        FROM document_chunks
        WHERE content_tokens IS NULL
        ORDER BY id
    """))).all()

    for row in rows:
        await conn.execute(
            text("UPDATE document_chunks SET content_tokens = :tokens WHERE id = :id"),
            {"tokens": tokenize_to_string(row.content), "id": row.id},
        )
    return len(rows)


async def verify_backfill(conn) -> None:
    """
    回填结果自查。三条断言，其中两条是**正向条件** ——
    防止"表里 0 行"这种空集合恒满足的假通过（铁律 9 的教训）。
    """
    missing = (await conn.execute(text(
        "SELECT count(*) FROM document_chunks WHERE content_tokens IS NULL"
    ))).scalar()
    if missing:
        raise RuntimeError(f"仍有 {missing} 行 content_tokens 为 NULL（回填没覆盖全）")

    rows = (await conn.execute(text("""
        SELECT id, coalesce(content, '') AS content, content_tokens
        FROM document_chunks ORDER BY id
    """))).all()

    # 断言 1：库里存的分词结果 == 现在重新分词的结果
    # 它能抓出「迁移漏回填」「有人手写数据库」「分词器换了但没重跑本脚本」三类问题
    for row in rows:
        recomputed = tokenize_to_string(row.content)
        if recomputed != (row.content_tokens or ""):
            raise RuntimeError(
                f"切片 {row.id} 的分词结果与库中不一致：库={row.content_tokens!r} 重算={recomputed!r}"
            )

    # 断言 2（正向）：有内容的切片，生成列必须非空 —— 证明生成列真的在工作
    broken = (await conn.execute(text("""
        SELECT count(*) FROM document_chunks
        WHERE content <> '' AND content_tsv IS NULL
    """))).scalar()
    if broken:
        raise RuntimeError(f"有 {broken} 行 content 非空但 content_tsv 为 NULL（生成列没生效）")

    # 断言 3（正向）：至少有一行分词后拿到了 token，否则说明分词器整个失效
    tokenized = (await conn.execute(text("""
        SELECT count(*) FROM document_chunks WHERE content_tokens <> ''
    """))).scalar()
    print(f"  [OK] 回填自查通过：共 {len(rows)} 行，其中 {tokenized} 行分词结果非空，"
          f"重新分词结果全部一致")


async def main() -> None:
    print("=" * 74)
    print("D16 迁移：document_chunks 分词列 + GIN 倒排索引 + 回填")
    print("=" * 74)
    # 整个迁移在一个事务里：任何一步自检失败 → 抛异常 → DDL 一起回滚，不留半成品
    async with engine.begin() as conn:
        print("[1/4] 应用 DDL")
        await apply_ddl(conn)
        print("[2/4] 回读库结构自查")
        await verify_schema(conn)
        print("[3/4] 回填历史切片")
        count = await backfill(conn)
        print(f"  [OK] 回填 {count} 行")
        print("[4/4] 回填结果自查")
        await verify_backfill(conn)

    await engine.dispose()
    print("=" * 74)
    print("迁移完成")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
