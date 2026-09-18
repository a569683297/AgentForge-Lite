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
        logger.info("① 删除旧的 documents 表（CASCADE 连带依赖对象）")
        await conn.execute(text("DROP TABLE IF EXISTS documents CASCADE"))

        logger.info("② 确保 vector 扩展存在")
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        logger.info("③ 按新模型建表（documents + document_chunks）")
        await conn.run_sync(Base.metadata.create_all)

        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY 1"
            )
        )
        tables = [row[0] for row in result]

    print()
    print("✅ 迁移完成，当前表：", ", ".join(tables))
    print("   下一步：脚本里的示例数据会在各自运行时自动重新入库")


if __name__ == "__main__":
    asyncio.run(main())
