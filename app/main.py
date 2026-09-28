"""
FastAPI 应用入口
================
启动顺序（入口 → 装配 → 返回）：
1. 加载配置（app.config.settings）
2. 初始化日志（app.core.logging）
3. 创建 FastAPI 实例，注册全局异常处理器
4. 注册路由（health 健康检查 + chat 对话 + documents 文档管理 + sessions 会话查询 + eval 评测）
5. 启动 uvicorn 后：
   - GET    http://localhost:8000/api/health
   - POST   http://localhost:8000/api/chat
   - POST   http://localhost:8000/api/documents        （上传）
   - GET    http://localhost:8000/api/documents        （列表）
   - DELETE http://localhost:8000/api/documents/{id}   （删除）
   - GET    http://localhost:8000/api/sessions                      （会话列表，D14）
   - GET    http://localhost:8000/api/sessions/{id}/messages        （会话历史，D14）
   - GET    http://localhost:8000/api/eval/cases                    （评测集列表，D21）
   - GET    http://localhost:8000/api/eval/cases/{case_key}         （单条详情，D21）
   - GET    http://localhost:8000/api/eval/dataset                  （评测集指纹，D21）
   - 交互式文档 http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.eval import router as eval_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.config import settings
from app.core.logging import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时执行一次，关闭时清理。"""
    logger.info("AgentForge 启动中 env=%s", settings.app_env)

    # Redis 启动自检：连不上只告警不崩溃（开发期容忍，D24 部署期可改为硬失败）
    from app.core.redis import ping as redis_ping

    if await redis_ping():
        logger.info("Redis 连接正常")
    else:
        logger.warning("Redis 连接失败，请检查 redis 容器是否启动")

    yield
    logger.info("AgentForge 已关闭")


app = FastAPI(
    title="AgentForge API",
    description="企业级 Agent 运行时与编排平台（阶段1：RAG+ 增强版）",
    version="0.1.0",
    lifespan=lifespan,
)


# ---- 全局异常处理：统一返回 JSON 错误，避免裸堆栈泄露 ----
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("未处理异常 path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "path": request.url.path},
    )


# ---- 路由注册 ----
app.include_router(health_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")   # D14 新增
app.include_router(eval_router, prefix="/api")       # D21 新增


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
