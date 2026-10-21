"""
工具服务器迁移：创建 tool_servers 表
=====================================
为什么单独一个脚本（而不是靠应用启动时 create_all）：
    本项目从 D12 起就有一条约定 —— **建表是显式动作，有输出、可复查**。
    启动时静默建表会让"表结构对不对"变成没人看过的事，
    而它恰好是最容易"语句成功、结果是错的"的地方（D21 的教训：
    ARRAY 列在 data_type 上只显示 "ARRAY"，元素类型藏在 udt_name 里）。

为什么必须回读库结构自查：
    `create_all(checkfirst=True)` 只按**表名**判断，表以任何形式存在就整张跳过。
    也就是说"命令没报错"完全不能证明列是对的。

幂等：可反复执行，已存在的表不会被改动。
运行（项目根目录）：
    uv run python -m scripts.tool_server_migrate
容器内：
    docker compose exec api uv run --no-sync python -m scripts.tool_server_migrate
"""

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401 —— 必须 import，否则模型不会注册到 Base.metadata
from app.core.db import Base, engine
from app.models.tool_server import ToolServer

# 期望的列结构：列名 → (data_type, udt_name)
#   ⚠ PG 的 information_schema 里，varchar 的 data_type 是 "character varying"，
#     真正的短名在 udt_name（"varchar"）。两列都要对，只对一个是自欺欺人。
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "id": ("uuid", "uuid"),
    "name": ("character varying", "varchar"),
    "display_name": ("character varying", "varchar"),
    "description": ("text", "text"),
    "entry_file": ("character varying", "varchar"),
    "sha256": ("character varying", "varchar"),
    "size_bytes": ("integer", "int4"),
    "status": ("character varying", "varchar"),
    "last_error": ("text", "text"),
    "created_at": ("timestamp with time zone", "timestamptz"),
    "updated_at": ("timestamp with time zone", "timestamptz"),
}

TABLE = ToolServer.__tablename__


async def create_table() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    print("  [OK] create_all 执行完毕（已存在的表不会被改动）")


async def verify_schema(conn) -> None:
    rows = (await conn.execute(text("""
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :t
    """), {"t": TABLE})).all()
    if not rows:
        raise RuntimeError(f"表 {TABLE} 不存在（create_all 没有建成）")

    found = {r.column_name: (r.data_type, r.udt_name) for r in rows}
    missing = set(EXPECTED_COLUMNS) - set(found)
    if missing:
        raise RuntimeError(f"表 {TABLE} 缺列：{sorted(missing)}")
    for column, want in EXPECTED_COLUMNS.items():
        if found[column] != want:
            raise RuntimeError(
                f"表 {TABLE}.{column} 类型不符：期望 (data_type, udt_name)={want}，"
                f"实际 {found[column]}"
            )
    print(f"  [OK] {TABLE}: {len(found)} 列，类型全部匹配")


async def verify_unique_name(conn) -> None:
    """
    name 的唯一约束**必须落到数据库上**。

    为什么这条是硬要求（不是"应用层查过了就行"）：
      `name` 同时是工具名前缀（注册表的 source）。两条记录同名 =
      两次 `register` 撞名 = 第二台 server 必然启动失败；
      更糟的是 `unregister_source(name)` 会把**两台**的工具一起摘掉。
      应用层的"先查再插"挡不住并发（两个请求同时查到"没有"）。
    """
    idx = (await conn.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = :t
    """), {"t": TABLE})).all()
    unique_on_name = [
        r.indexdef for r in idx
        if "UNIQUE" in r.indexdef.upper() and "name" in r.indexdef
    ]
    if not unique_on_name:
        raise RuntimeError(
            f"{TABLE}.name 没有唯一约束，现有索引：{[r.indexdef for r in idx]}"
        )
    print(f"  [OK] name 唯一约束存在：{unique_on_name[0]}")


async def verify_behavior(conn) -> None:
    """
    行为自查：真的插两条同名会怎样。

    结构自查能证明"有个唯一索引"，证明不了"它真的会拒绝重复"。
    这里在 savepoint 里插两条同名，断言第二条被拒，然后回滚 —— **不留任何数据**。
    """
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text("""
            INSERT INTO tool_servers (id, name, display_name, description, entry_file,
                                      sha256, size_bytes, status)
            VALUES (gen_random_uuid(), 'migrate_probe', '探针', '', 'probe/server.py',
                    repeat('0', 64), 1, 'active')
        """))
        try:
            await conn.execute(text("""
                INSERT INTO tool_servers (id, name, display_name, description, entry_file,
                                          sha256, size_bytes, status)
                VALUES (gen_random_uuid(), 'migrate_probe', '探针2', '', 'probe2/server.py',
                        repeat('1', 64), 1, 'active')
            """))
        except Exception as e:  # noqa: BLE001 —— 这里**期望**它失败
            name = type(e).__name__
            if "UniqueViolation" not in name and "IntegrityError" not in name:
                raise RuntimeError(f"同名插入被拒了，但不是唯一约束导致的：{name}: {e}") from e
            print(f"  [OK] 同名第二条被库拒绝（{name}）—— 约束真的生效")
        else:
            raise RuntimeError("同名第二条竟然插入成功：唯一约束没起作用")
    finally:
        await savepoint.rollback()

    left = (await conn.execute(text(
        "SELECT count(*) AS n FROM tool_servers WHERE name = 'migrate_probe'"
    ))).scalar_one()
    if left != 0:
        raise RuntimeError(f"探针数据残留 {left} 条，回滚没生效")
    print("  [OK] 探针已回滚，无残留")


async def verify_metadata_match(conn) -> None:
    """交叉核对：ORM 声明的列 == 库里的列（抓"改了模型忘了迁移"）。"""
    declared = {c.name for c in ToolServer.__table__.columns}
    actual = {
        r.column_name for r in (await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t
        """), {"t": TABLE})).all()
    }
    if declared != actual:
        raise RuntimeError(
            f"{TABLE} ORM 与库不一致：ORM 多出 {sorted(declared - actual)}，"
            f"库里多出 {sorted(actual - declared)}"
        )
    print(f"  [OK] {TABLE} ORM 声明与库结构一致（{len(declared)} 列）")


async def main() -> None:
    print("=" * 74)
    print("迁移：创建 tool_servers（用户上传的 MCP Server）")
    print("=" * 74)
    print("[1/4] 建表")
    await create_table()
    print("[2/4] 回读库结构自查")
    async with engine.begin() as conn:
        await verify_schema(conn)
    print("[3/4] 唯一约束（结构 + 行为）")
    async with engine.begin() as conn:
        await verify_unique_name(conn)
    async with engine.begin() as conn:
        await verify_behavior(conn)
    print("[4/4] ORM 声明与库结构交叉核对")
    async with engine.begin() as conn:
        await verify_metadata_match(conn)

    await engine.dispose()
    print("=" * 74)
    print("迁移完成")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
