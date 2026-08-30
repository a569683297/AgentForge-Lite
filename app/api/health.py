"""
健康检查接口
============
GET /api/health —— 返回服务状态及各依赖连接情况。

设计说明：
- Docker 编排（D24）用 /api/health 做容器健康检查探针
- 实时探测各依赖连通性（redis），依赖挂掉要能看出来
- 注意：探活用超时保护（D25 起可加），避免健康检查本身卡死
"""

from fastapi import APIRouter

from app.core.redis import ping as redis_ping

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """
    健康检查。返回：
    {
      "status": "ok",
      "version": "0.1.0",
      "deps": {"api": "ok", "redis": "ok"}
    }
    """
    redis_ok = await redis_ping()
    deps = {
        "api": "ok",
        "redis": "ok" if redis_ok else "down",
    }
    # 整体状态：任一关键依赖挂了 → degraded（部分可用）
    status = "ok" if redis_ok else "degraded"
    return {
        "status": status,
        "version": "0.1.0",
        "deps": deps,
    }
