"""
数据库连接层
============
ORM 四件套中的三个核心在此定义：engine / sessionmaker / Base。

端到端流程（每次操作数据库）：
1. 应用启动时创建 engine（连接池，复用连接）
2. 每次请求需要数据库时：从 sessionmaker 拿一个 Session
3. 在 Session 上执行增删改查（session.add / await session.commit）
4. commit 时 SQLAlchemy 把 Python 操作翻译成 SQL，交给 engine 执行
5. 用完关闭 Session（归还连接，防泄漏）

类比：engine = 连接池（axios 实例），Session = 一次事务工作区（request context）。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


# ---- 1. engine：负责真正连接数据库，管理连接池 ----
# pool_pre_ping=True：取出连接前先 ping 一下，防止拿到已断开的连接（数据库重启后必用）
engine = create_async_engine(
    settings.database_url,
    echo=False,              # True 会打印所有 SQL（调试用，生产关掉）
    pool_pre_ping=True,
)


# ---- 2. sessionmaker：创建 Session 的"工厂" ----
# expire_on_commit=False：commit 后对象属性仍然可读（否则提交后访问属性会再查一次库）
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ---- 3. Base：所有 ORM 模型的基类 ----
# 模型类继承它，SQLAlchemy 才知道"这是一个表定义"
class Base(DeclarativeBase):
    pass


# ---- 4. FastAPI 依赖：每个请求一个 Session，用完自动关闭 ----
# 用法：def endpoint(db: AsyncSession = Depends(get_session)):
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """依赖注入：请求进来时创建 Session，请求结束自动关闭。"""
    async with async_session_factory() as session:
        yield session
