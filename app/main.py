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
   - GET    http://localhost:8000/api/tools                         （工具清单+来源，D27）
   - GET    http://localhost:8000/api/tool-servers                  （用户上传的工具，管理视角）
   - POST   http://localhost:8000/api/tool-servers                  （上传 MCP Server .py，202）
   - POST   http://localhost:8000/api/tool-servers/{id}/call        （试跑其一个工具）
   - POST   http://localhost:8000/api/tool-servers/{id}/reconnect   （重连）
   - GET/POST/PATCH/DELETE http://localhost:8000/api/models         （模型注册表，模型切换 S1）
   - GET    http://localhost:8000/api/metrics                       （指标清单，语义层）
   - POST   http://localhost:8000/api/metrics/query                 （指标查询，语义层）
   - GET    http://localhost:8000/api/traces                        （真实 trace 列表）
   - GET    http://localhost:8000/api/traces/{trace_id}             （真实 span 树）
   - POST   http://localhost:8000/api/auth/register                 （注册，2026-10-21）
   - POST   http://localhost:8000/api/auth/login                    （登录）
   - POST   http://localhost:8000/api/auth/refresh                  （刷新令牌，轮换）
   - POST   http://localhost:8000/api/auth/logout                   （登出）
   - GET    http://localhost:8000/api/auth/me                       （当前用户）
   - POST   http://localhost:8000/api/auth/change-password          （改密）
   - POST   http://localhost:8000/api/auth/forgot-password           （申请重置链接）
   - POST   http://localhost:8000/api/auth/reset-password           （用令牌重置）
   - GET    http://localhost:8000/api/admin/users                   （用户列表，管理员）
   - PATCH  http://localhost:8000/api/admin/users/{id}              （禁用/改角色，管理员）
   - GET    http://localhost:8000/api/admin/audit                   （审计日志，管理员）
   - GET    http://localhost:8000/api/profile                       （我的画像，2026-10-21）
   - PATCH  http://localhost:8000/api/profile                       （改显式字段：没传=保持，空串=删除）
   - DELETE http://localhost:8000/api/profile/facts/{id}            （删一条自动提取的事实）
   - 交互式文档 http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.eval import router as eval_router
from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.models import router as models_router
from app.api.profile import router as profile_router
from app.api.retriever import router as retriever_router
from app.api.sessions import router as sessions_router
from app.api.tool_servers import router as tool_servers_router
from app.api.tools import router as tools_router
from app.api.traces import router as traces_router
from app.config import settings
from app.core.logging import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时执行一次，关闭时清理。"""
    logger.info("AgentForge 启动中 env=%s", settings.app_env)

    # 账号体系自检：签名密钥没配就明确警告一次。
    # 它不会让服务起不来（开发期要能跑），但"重启后全员掉登录"这种事
    # 必须在启动日志里就看得见 —— 否则它会在部署后变成一个离奇的故障。
    from app.core.security import warn_if_insecure

    warn_if_insecure()

    # Redis 启动自检：连不上只告警不崩溃（开发期容忍，D24 部署期可改为硬失败）
    from app.core.redis import ping as redis_ping

    if await redis_ping():
        logger.info("Redis 连接正常")
    else:
        logger.warning("Redis 连接失败，请检查 redis 容器是否启动")

    # D27：接入外部 MCP Server（动态发现工具 → 注册进 ToolRegistry）
    # ⚠ 这里**不能**让接入失败拖死启动：外部 server 要网络/要 npm/要 PAT，
    #   全是本应用控制不了的东西。失败由 manager 收敛成 status，只记日志。
    if settings.mcp_enabled:
        from app.mcp import build_specs, get_manager

        # ⚠ start() 返回的是 dict[str, dict]（按 server 名索引），
        #   直接迭代 dict 拿到的是**键**，不是值 —— 第一次写成了 `for s in status`，
        #   于是 `s["connected"]` 报 TypeError: string indices must be integers。
        #   是这个错误让 lifespan 崩掉、TestClient 起不来，被 d27_verify 的 E 段抓出来的。
        status = await get_manager().start(build_specs(settings))
        ok = [name for name, s in status.items() if s["connected"]]
        bad = [name for name, s in status.items() if not s["connected"]]
        logger.info("MCP 接入完成 成功=%s 失败=%s", ok or "无", bad or "无")

    # ---- 工具沙箱（用户上传的 MCP Server）----
    # 三件事，顺序不能换：先清扫尸体 → 再校验镜像 → 最后按库恢复。
    # 清扫必须在前：上一次崩溃留下的容器占着名字，恢复时会撞名失败。
    if settings.tool_sandbox_enabled:
        import asyncio

        from app.mcp import sandbox
        from app.services import tool_server_service

        try:
            await asyncio.to_thread(sandbox.sweep_orphans)
        except Exception as e:  # noqa: BLE001 —— 清扫失败不该阻止启动
            logger.warning("清理残留沙箱容器失败（忽略）：%s", e)

        # 恢复失败只告警：一台用户工具起不来，不能拖死整个 API。
        # （与上面 MCP 接入同一条原则，只是换到用户维度。）
        try:
            restored = await tool_server_service.restore_active()
            logger.info("工具 server 恢复结果 %s", restored)
        except Exception as e:  # noqa: BLE001
            logger.warning("工具 server 恢复失败（忽略）：%s", e)

    yield

    # D27：断开全部 MCP server。
    # 不主动 kill 子进程 —— 断开 stdin 即为 EOF，子进程自行退出（D26 实测：
    # `server.run()` 阻塞但会正常返回，生命周期归宿主）。
    # 同时会把各 server 的工具从注册表摘掉，避免留下"调不通的工具"。
    from app.mcp import get_manager

    await get_manager().stop()
    logger.info("AgentForge 已关闭")


app = FastAPI(
    title="AgentForge API",
    description="企业级 Agent 运行时与编排平台（阶段1：RAG+ 增强版）",
    version="0.1.0",
    lifespan=lifespan,
)


# ---- 输入边界：路径里出现 NUL 字节（%00）直接 400 ----
@app.middleware("http")
async def reject_nul_byte_in_path(request: Request, call_next):
    """
    请求路径含 NUL 字节时直接 400，**不让它走到下游**。

    为什么必须在这里拦：NUL 不是合法的 UTF-8 文本，PostgreSQL 会抛
        asyncpg.exceptions.CharacterNotInRepertoireError:
        invalid byte sequence for encoding "UTF8": 0x00
    参数化查询**救不了**它 —— 这不是 SQL 注入（值没有被拼进 SQL），
    而是"这个字符本身就非法"。不拦的话它会以 **500** 的形式冒出来，
    把一个"用户输入问题"伪装成"服务端故障"，污染错误监控。

    查两处是因为它们**不一定相同**：
      · `request.url.path` 是 Starlette 解码后的路径
      · `scope["raw_path"]` 是**原始未解码**的字节串
    只查前者，某些百分号编码写法（如 `%2500`）会漏过去。
    （本用例由 tests/security/test_injection.py 发现 —— 测试驱动出来的修复。）
    """
    if "\x00" in request.url.path or b"%00" in request.scope.get("raw_path", b"").lower():
        logger.warning("拒绝含 NUL 字节的请求路径 path=%r", request.url.path)
        return JSONResponse(status_code=400, content={"detail": "请求路径含非法字符"})
    return await call_next(request)


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
app.include_router(auth_router, prefix="/api")     # 账号体系（注册/登录/刷新/找回）
app.include_router(admin_router, prefix="/api")    # 管理员（用户管理 + 审计日志）
app.include_router(chat_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")   # D14 新增
app.include_router(eval_router, prefix="/api")       # D21 新增
app.include_router(tools_router, prefix="/api")      # D27 新增（含动态发现的 MCP 工具）
app.include_router(tool_servers_router, prefix="/api")  # 用户上传的 MCP Server（管理面）
app.include_router(models_router, prefix="/api")     # 模型切换 S1 新增（模型注册表）
app.include_router(metrics_router, prefix="/api")    # 语义层新增（指标清单 + 查询）
app.include_router(traces_router, prefix="/api")     # Trace 页真实数据（Langfuse v2 观测）
app.include_router(retriever_router, prefix="/api")  # 检索检查器（候选池 + 重排 + gold 判定）
app.include_router(profile_router, prefix="/api")    # 用户画像（跨会话记忆，2026-10-21）


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
