"""
模型切换 S1 迁移：创建 llm_models 表
======================================
编号说明：借 D40 缓冲位的场合（10-12 的《本地模型接入方案》提过"可占 D40 缓冲位"，
本次按此执行）。它不属于 39 天核心计划，是"M6 优化批次"的第一个建表需求。
若后续另有编号安排，整体改名只影响文件名，**不影响表名与任何代码**。

建表方式与 d21_migrate 同款：**新建表**用 create_all（改已有表才需要手写 DDL），
但**仍然必须回读库结构自查**，因为：
  · create_all 的 checkfirst 只按表名判断，表以任何形式存在就跳过；
  · `server_default=text("true")` 这类库级默认值到底有没有被正确处理，
    "语句成功"说明不了任何事 —— 只有回读 column_default 才知道（D21 教训）。

幂等性：create_all(checkfirst=True) 可反复执行；已存在的表不会被改动。
运行（必须在项目根目录）：
    cd <项目根> && ~/.local/bin/uv run python -m scripts.d40_migrate
"""

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401 —— 必须 import，否则模型不会注册到 Base.metadata
from app.core.db import Base, engine
from app.models.llm_model import LlmModel

# 期望的列结构：列名 → (data_type, udt_name)
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "llm_models": {
        "id": ("uuid", "uuid"),
        "name": ("character varying", "varchar"),
        "base_url": ("character varying", "varchar"),
        "api_key": ("character varying", "varchar"),
        "model": ("character varying", "varchar"),
        "enabled": ("boolean", "bool"),
        "created_at": ("timestamp with time zone", "timestamptz"),
        "updated_at": ("timestamp with time zone", "timestamptz"),
    },
}


async def create_tables() -> None:
    # 只建"还没建的表"，不碰已有的任何一张（checkfirst 的语义）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    print("  [OK] create_all 执行完毕（已存在的表不会被改动）")


async def verify_schema(conn) -> None:
    """
    回读**数据库的真实结构**自查（不信"语句没报错"——见模块 docstring）。
    """
    for table, expected in EXPECTED_COLUMNS.items():
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT column_name, data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :t
                    """
                ),
                {"t": table},
            )
        ).all()
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

    # enabled 的库级默认值实际长什么样。
    # `server_default=text("true")` 有没有被正确翻译成 DEFAULT true —— 只有回读才知道。
    defaults = (
        await conn.execute(
            text(
                """
                SELECT column_name, column_default FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'llm_models'
                  AND column_default IS NOT NULL
                ORDER BY column_name
                """
            )
        )
    ).all()
    print(f"  ℹ llm_models 带库级默认值的列：{ {r.column_name: r.column_default for r in defaults} }")
    enabled_default = next((r.column_default for r in defaults if r.column_name == "enabled"), None)
    if enabled_default != "true":
        raise RuntimeError(f"llm_models.enabled 的库级默认值应为 true，实际 {enabled_default!r}")


async def verify_metadata_match(conn) -> None:
    """
    交叉核对：ORM 声明的列 == 库里的列。
    能抓到"模型改了但忘了跑迁移"这类静默问题（应用照常启动，字段永远读出来是 None）。
    """
    declared = {c.name for c in LlmModel.__table__.columns}
    actual = {
        r.column_name
        for r in (
            await conn.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'llm_models'
                    """
                )
            )
        ).all()
    }
    if declared != actual:
        raise RuntimeError(
            f"llm_models ORM 与库不一致："
            f"ORM 多出 {sorted(declared - actual)}，库里多出 {sorted(actual - declared)}"
        )
    print(f"  [OK] llm_models ORM 声明与库结构一致（{len(declared)} 列）")


async def main() -> None:
    print("=" * 74)
    print("模型切换 S1 迁移：创建 llm_models（自定义模型注册表）")
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
    print("[DONE] llm_models 就绪（二次执行应输出相同的列数、且无任何新增）")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
