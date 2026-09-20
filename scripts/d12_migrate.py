"""
D12 迁移：documents 单表 → documents + document_chunks 双表
=============================================================
为什么直接 drop 重建，而不是写 Alembic 迁移：
  旧表里只有验证脚本灌进去的测试数据（d10-e2e、d11-alpha.md 等），
  没有需要保留的东西。为它写一套「把散落的切片行聚合成文档行、
  再回填 document_id 外键」的数据迁移，纯属浪费。

  这是工程判断，不是通用做法：**练习期的测试数据不值得为它写迁移**。
  真实生产环境必须保数据，那时用 Alembic + 数据回填脚本一步步走。

执行：
    uv run python -m scripts.d12_migrate
"""

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401  导入即把所有模型注册到 Base.metadata
from app.core.db import Base, engine
from app.core.logging import logger


async def main() -> None:
    async with engine.begin() as conn:
        logger.info("① 删除旧表（先子后父：document_chunks → documents）")
        # ⚠️ 两张表都要显式 drop，不能只删父表靠 CASCADE 连带。
        #    事故复盘（2026-09-18）：旧版本只写了 DROP TABLE documents CASCADE，
        #    而当时 document_chunks 表处于「没有外键」的坏状态 ——
        #    没有外键就不是依赖对象，CASCADE 连带不到它，表会活下来；
        #    随后 create_all 因 checkfirst 默认 True 看到表已存在直接跳过，
        #    外键永远补不上 → 删文档留下孤儿切片（V12-V13 失败的真因）。
        await conn.execute(text("DROP TABLE IF EXISTS document_chunks CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS documents CASCADE"))

        logger.info("② 确保 vector 扩展存在")
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        logger.info("③ 按新模型建表（documents + document_chunks）")
        await conn.run_sync(Base.metadata.create_all)

        logger.info("④ 结构自检：确认级联外键真的建出来了")
        # create_all 不是迁移工具 —— 表已存在就跳过，模型改了它也不管。
        # 所以建完表必须自己验一遍「约束是否真的在」，否则又是一次静默失败。
        fk_defs = [
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = 'document_chunks'::regclass AND contype = 'f'"
                    )
                )
            ).all()
        ]
        cascade_fks = [d for d in fk_defs if "ON DELETE CASCADE" in d]
        if not cascade_fks:
            # 抛异常 → engine.begin() 整个事务回滚，库里不会留下半成品结构
            raise RuntimeError(
                "结构自检失败：document_chunks 没有带 ON DELETE CASCADE 的外键，"
                f"删文档会留下孤儿切片。实际外键：{fk_defs or '无'}"
            )

        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY 1"
            )
        )
        tables = [row[0] for row in result]

    print()
    print("✅ 迁移完成，当前表：", ", ".join(tables))
    print("✅ 结构自检通过：", cascade_fks[0])
    print("   下一步：脚本里的示例数据会在各自运行时自动重新入库")


if __name__ == "__main__":
    asyncio.run(main())
