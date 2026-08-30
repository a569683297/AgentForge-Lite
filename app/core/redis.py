"""
Redis 客户端模块
================
项目全局共用一个 Redis 连接实例（单例），业务代码从这导入。

为什么用 redis.asyncio：
- FastAPI 全异步，Redis 操作也必须异步（await），否则阻塞事件循环
- 类比前端：axios 用 async，不能阻塞 UI 线程

decode_responses=True 的作用：
- Redis 存的是 bytes（二进制），默认读取返回 bytes（如 b"hello"）
- 设为 True 后自动解码成 str（"hello"），省去手动 .decode() 的麻烦
"""

from redis.asyncio import Redis

from app.config import settings


# 单例连接实例：整个应用共享
# 注意：这里只是"创建连接对象"，真正连接发生在第一次操作时（懒连接）
redis_client: Redis = Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    decode_responses=True,   # bytes → str 自动解码
)


async def ping() -> bool:
    """健康检查用：测 Redis 是否连通。"""
    try:
        return bool(await redis_client.ping())
    except Exception:
        return False
