"""
FastAPI 应用入口
================
启动顺序（入口 → 装配 → 返回）：
1. 加载配置（app.config.settings）
2. 初始化日志（app.core.logging）
3. 创建 FastAPI 实例，注册全局异常处理器
4. 注册路由（当前只有 health，后续 D2+ 逐个挂载 api 子路由）
5. 启动 uvicorn 即可访问 http://localhost:8000/health
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.config import settings
from app.core.logging import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时执行一次，关闭时清理。"""
    logger.info("AgentForge 启动中 env=%s", settings.app_env)
    # D2 起在这里初始化数据库连接池 / Redis
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
