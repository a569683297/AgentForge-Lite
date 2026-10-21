"""
RAG 上传健壮性迁移：documents 补内容指纹 + 状态时间戳 + 唯一索引
==============================================================
为什么必须写成迁移脚本，而不是靠 `Base.metadata.create_all()`：
    铁律 10 —— create_all(checkfirst=True) 见到表已存在就**整张跳过**，
    不补列、不改类型、不建索引（d16_migrate.py 的注释里踩过这个坑）。
    所以这里用显式 DDL，做完立刻回读库结构自查，自查不过整体回滚。

三个动作：
    ① 加列 `content_hash VARCHAR(64)` —— sha256(原始文件字节)，幂等去重的身份
    ② 加列 `updated_at timestamptz`     —— 状态机时间戳（块 2 的 processing 超时回收要用）
    ③ 建唯一索引 `uq_documents_content_hash` —— 并发下的最后一道防线

关于历史数据：`content_hash` 可空，老记录保持 NULL。PostgreSQL 的唯一索引
**允许多个 NULL**，所以它们之间不会互相冲突 —— 但它们也**不参与去重**。
原始文件从没落过盘，回填不出来，这是必须承认的事实，不是能绕过去的实现细节。

幂等性：DDL 全部带 IF NOT EXISTS，可重复执行。
校验部分每次都用**临时探针行**验证约束真的生效（在 savepoint 里插入后回滚，不留数据），
而不是只看"语句没报错" —— ALTER ... IF NOT EXISTS 在列已存在时是静默跳过的。

运行（必须在项目根目录）：
    cd <项目根> && .venv/bin/python -m scripts.rag_upload_migrate
    # 容器内：docker exec agentforge-api python -m scripts.rag_upload_migrate
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import engine

DDL_STATEMENTS: list[tuple[str, str]] = [
    (
        "加列 content_hash(VARCHAR(64))",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
    ),
    (
        "加列 updated_at(timestamptz，带默认值)",
        """
        ALTER TABLE documents
        ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()
        """,
    ),
    (
        "建唯一索引 uq_documents_content_hash",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_content_hash
        ON documents (content_hash)
        """,
    ),
]

# 自查用的期望结构：列名 → (data_type, is_nullable)
EXPECTED_COLUMNS = {
    "content_hash": ("character varying", "YES"),   # 历史数据允许 NULL
    "updated_at": ("timestamp with time zone", "NO"),
}


async def apply_ddl(conn) -> None:
    for label, ddl in DDL_STATEMENTS:
        await conn.execute(text(ddl))
        print(f"  [OK] {label}")


async def verify_schema(conn) -> None:
    """回读 information_schema / pg_indexes，确认列与索引**真的**是这个形状。"""
    rows = (await conn.execute(text("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'documents'
          AND column_name IN ('content_hash', 'updated_at')
    """))).all()
    found = {r.column_name: (r.data_type, r.is_nullable) for r in rows}

    for column, expected in EXPECTED_COLUMNS.items():
        actual = found.get(column)
        if actual != expected:
            raise RuntimeError(
                f"列 {column} 结构不符：期望 (data_type, is_nullable)={expected}，实际 {actual}"
            )
    print(f"  [OK] 列结构自查通过：{found}")

    indexdef = (await conn.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE tablename = 'documents' AND indexname = 'uq_documents_content_hash'
    """))).scalar()
    if not indexdef or "unique" not in indexdef.lower() or "content_hash" not in indexdef.lower():
        raise RuntimeError(f"唯一索引未生效，pg_indexes 里查到的是：{indexdef!r}")
    print(f"  [OK] 索引自查通过：{indexdef}")


async def _insert_probe(conn, filename: str, content_hash: str | None) -> None:
    """插入一行一次性探针记录（调用方负责用 savepoint 回滚，绝不提交）。"""
    await conn.execute(
        text("""
            INSERT INTO documents (id, filename, file_type, content_hash)
            VALUES (:id, :filename, 'txt', :content_hash)
        """),
        {"id": str(uuid.uuid4()), "filename": filename, "content_hash": content_hash},
    )


async def verify_uniqueness(conn) -> None:
    """
    用探针行验证唯一约束**真的在拦截**，而不是"索引名字在 pg_indexes 里"。

    两条断言：
      ① 同一 hash 插第二次 → 必须抛 IntegrityError
      ② 多个 NULL 同时存在 → 必须允许（历史数据共存的前提）
    都在 savepoint 里做，最后整体回滚 —— 不会在 documents 表里留下任何东西。
    """
    probe_hash = "0" * 64
    before = (await conn.execute(text("SELECT count(*) FROM documents"))).scalar()

    # ① 同 hash 第二次插入必须失败
    savepoint = await conn.begin_nested()
    try:
        await _insert_probe(conn, "rag-migrate-probe-a.txt", probe_hash)
        await _insert_probe(conn, "rag-migrate-probe-b.txt", probe_hash)
    except IntegrityError:
        await savepoint.rollback()
        print("  [OK] 唯一约束生效：相同 content_hash 的第二次插入被拒绝")
    else:
        await savepoint.rollback()
        raise RuntimeError("唯一约束未生效：两条相同 content_hash 的记录都被插进去了")

    # ② 多个 NULL 必须允许（唯一索引不约束 NULL，历史数据才能共存）
    savepoint = await conn.begin_nested()
    try:
        await _insert_probe(conn, "rag-migrate-probe-null-1.txt", None)
        await _insert_probe(conn, "rag-migrate-probe-null-2.txt", None)
        print("  [OK] NULL 指纹可共存：多条历史数据（content_hash 为空）不会互相冲突")
    finally:
        await savepoint.rollback()

    after = (await conn.execute(text("SELECT count(*) FROM documents"))).scalar()
    if after != before:
        raise RuntimeError(f"探针行泄漏：迁移前 {before} 行，迁移后 {after} 行")
    print(f"  [OK] 探针已全部回滚，documents 行数未变（{before} 行）")


async def main() -> None:
    print("=" * 74)
    print("迁移：documents 内容指纹 + 状态时间戳 + 唯一索引")
    print("=" * 74)
    # 整个迁移在一个事务里：任何一步自检失败 → 抛异常 → DDL 一起回滚，不留半成品
    async with engine.begin() as conn:
        print("[1/4] 应用 DDL")
        await apply_ddl(conn)
        print("[2/4] 回读库结构自查")
        await verify_schema(conn)
        print("[3/4] 唯一约束行为验证")
        await verify_uniqueness(conn)
        print("[4/4] 完成")

    await engine.dispose()
    print("=" * 74)
    print("迁移完成")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
