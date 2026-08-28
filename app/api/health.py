"""
健康检查接口
============
GET /api/health —— 返回服务状态及各依赖连接情况。

设计说明：
- Docker 编排（D24）用 /api/health 做容器健康检查探针
- 当前只有 API 自身状态；D2/D4 接数据库、Redis 后，
  在这里追加 pg/redis 连通性检查（依赖挂掉要能看出来）
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """
    健康检查。返回：
    {
      "status": "ok",
      "version": "0.1.0",
      "deps": {"api": "ok"}   # D2+ 追加 pg/redis
    }
    """
    return {
        "status": "ok",
        "version": "0.1.0",
        "deps": {
            "api": "ok",
        },
    }
