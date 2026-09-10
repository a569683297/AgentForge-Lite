"""
建表脚本
========
用法：uv run python -m scripts.init_db
作用：根据 ORM 模型创建所有表（开发期用 create_all；生产用 Alembic 迁移，阶段3 引入）

为什么开发期用 create_all 而不是 Alembic：
- create_all 只创建不存在的表，不处理改表结构 —— 够开发用
- Alembic 是正式迁移工具（能记录/回滚表结构变更），阶段3 换
"""

import asyncio

from sqlalchemy import text

from app.core.db import Base, engine
from app.core.logging import logger
import app.models  # noqa: F401  确保模型注册到 Base.metadata


async def init_db() -> None:
    """创建所有表。幂等：已存在的表跳过。"""
    async with engine.begin() as conn:
        # ⚠️ 必须先启用 pgvector 扩展，否则 documents 表的 vector 列类型不存在
        # （D1 的 docker-compose 用的就是 pgvector/pgvector:pg16 镜像，扩展可用）
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("pgvector 扩展已启用")

        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表创建完成")


if __name__ == "__main__":
    asyncio.run(init_db())
