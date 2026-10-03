"""
MCP 连接管理器（D27）
=====================
`MCPClient` 管**一台** server；`MCPManager` 管**一批** —— 并且管理它们的生死。

三件职责，每一件都对应一个真实的坑：
1. **批量启动，单个失败不拖垮全局** —— 外部 server 是我们控制不了的进程
   （要网络、要 npm、要 PAT）。它起不来，我们的 API 也必须能起来。
   → 失败记 `status=failed` + 原因，继续启动下一个。
2. **断开时把工具一起摘掉** —— 这是 `unregister_source` 存在的理由。
   不摘的话，`_registry` 里会留下"调不通的工具"，LLM 照旧会去调（不报错）。
3. **提供可观测的状态** —— `/api/tools` 要能回答"这台 server 现在什么情况"。

生命周期归谁（D26 实测结论）：**归宿主**。
   子进程读 stdin，客户端断开 → EOF → 自行退出。我们不 kill，也不会留孤儿。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.core.logging import logger
from app.mcp.client import MCPClient, MCPServerSpec
from app.tools.registry import unregister_source

# 项目根（app/mcp/manager.py → app/mcp → app → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 给子进程的**基础白名单**：只给"进程能正常跑起来"所必需的两样。
# ⚠ 绝不用 `{**os.environ}`：宿主环境里任何一个同名变量都可能让子进程读错配置
#   （实测：`.env` 的 `LOG_LEVEL=INFO` 让 harness 启动即 Fatal error）。
_BASE_ENV_KEYS = ("PATH", "HOME", "TMPDIR")


def _base_env() -> dict[str, str]:
    """取基础白名单环境（存在才取，缺了就不放，避免塞空串覆盖对方默认值）。"""
    return {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}


class MCPManager:
    """一批 MCP server 的连接池 + 生命周期。模块级单例，见 `get_manager()`。"""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._status: dict[str, dict] = {}

    # ---------------- 启动 ----------------
    async def start(self, specs: list[MCPServerSpec]) -> dict[str, dict]:
        """按顺序连接全部 server。**任何一台失败都不影响其它台与应用本身。**"""
        for spec in specs:
            await self.add_server(spec)
        return dict(self._status)

    async def add_server(self, spec: MCPServerSpec) -> dict:
        """
        接入一台 server（已存在则先断开再重接）。

        返回这一台的状态字典。
        """
        if spec.name in self._clients:
            logger.info("MCP server「%s」已存在，先断开再重接", spec.name)
            await self.remove_server(spec.name)

        client = MCPClient(spec)
        try:
            await client.connect(timeout_s=spec_timeout(spec))
            names = await client.discover_and_register()
        except Exception as e:  # noqa: BLE001
            # 这里**必须**吞掉异常，只记状态。
            # 一个外部依赖起不来，不该让整个 AgentForge 起不来（同 lifespan 里
            # Redis 自检"只告警不崩溃"的口径）。但**不能静默** ——
            # 状态里留 reason，`/api/tools` 一眼能看到是哪台、为什么没上来。
            detail = _explain_connect_failure(spec, e)
            self._status[spec.name] = {
                "name": spec.name,
                "connected": False,
                "tools": [],
                "error": detail,
                "stderr_log": str(client.stderr_path) if client.stderr_path else None,
            }
            logger.warning("MCP server「%s」接入失败：%s", spec.name, detail)
            await client.close()
            return self._status[spec.name]

        self._clients[spec.name] = client
        self._status[spec.name] = {
            "name": spec.name,
            "connected": True,
            "tools": names,
            "tool_count": len(names),
            "server_info": (
                {
                    "name": getattr(client.server_info, "name", None),
                    "version": getattr(client.server_info, "version", None),
                }
                if client.server_info is not None
                else None
            ),
            "protocol_version": client.protocol_version,
            "stderr_log": str(client.stderr_path) if client.stderr_path else None,
            "error": None,
        }
        return self._status[spec.name]

    # ---------------- 断开 ----------------
    async def remove_server(self, name: str) -> list[str]:
        """断开一台 server，并**把它的工具从注册表摘掉**。返回被摘掉的名字。"""
        client = self._clients.pop(name, None)
        removed = unregister_source(name)
        if client is not None:
            await client.close()
        self._status.pop(name, None)
        if removed:
            logger.info("MCP server「%s」已断开，摘除工具 %d 个：%s", name, len(removed), removed)
        return removed

    async def stop(self) -> None:
        """应用关闭时统一回收（逆序，先接的后断）。"""
        for name in reversed(list(self._clients.keys())):
            await self.remove_server(name)

    # ---------------- 查询 ----------------
    def status(self) -> list[dict]:
        """每台 server 的现状（含失败原因），给 `/api/tools` 与排障用。"""
        return list(self._status.values())

    def connected_names(self) -> list[str]:
        return list(self._clients.keys())


# ---------------- 模块级单例 ----------------
_manager = MCPManager()


def get_manager() -> MCPManager:
    """取全局管理器（lifespan 启停它，接口层查它的状态）。"""
    return _manager


def spec_timeout(spec: MCPServerSpec) -> float:
    """
    给不同 server 不同的连接超时。

    为什么要分：`npx` 这类命令**可能现下载依赖**（实测：直连 npm 官方源会挂住
    5 分钟以上，加国内镜像后 2 分 31 秒），而下文自研 server 是本机 python 起进程、
    毫秒级。用同一个超时值必然二选一：要么拖死本机 server 的启动，要么杀了 npx。
    """
    return 240.0 if spec.command.endswith("npx") else 20.0


def _explain_connect_failure(spec: MCPServerSpec, exc: Exception) -> str:
    """
    把连不上的原因拼成一句**人能直接照着查**的话。

    特别是异常组（anyio 的 TaskGroup 会把子异常包成 `ExceptionGroup`），
    直接 `str()` 只会得到 "unhandled errors in a TaskGroup (1 sub-exception)" ——
    真正的原因藏在里面，排障时等于没有信息。这里把它**摊平**。
    """
    leaf = exc
    while getattr(leaf, "exceptions", None):     # ExceptionGroup / BaseExceptionGroup
        leaf = leaf.exceptions[0]
    return f"{type(leaf).__name__}: {leaf}"


def build_specs(settings) -> list[MCPServerSpec]:
    """
    从配置组装要接入的 server 清单。

    分成两台，各自代表一类真实形态：
    - **inventory**：本机自研（D26 产物），进程内数据、零外部依赖 → 永远该在
    - **harness**：真实外部企业系统，要网络 + npm + PAT → **默认关**，按需开
    """
    specs: list[MCPServerSpec] = []

    specs.append(
        MCPServerSpec(
            name="inventory",
            command=sys.executable,        # 与主进程同一个解释器 → 一定能 import 到 mcp
            args=[str(PROJECT_ROOT / "mcp_servers" / "inventory_server.py")],
            env={**_base_env(), "LOG_LEVEL": "info"},
            namespace="inventory",         # 远端 query_inventory → inventory_query_inventory
            cwd=str(PROJECT_ROOT),
        )
    )

    if settings.mcp_harness_enabled:
        if not settings.harness_api_key:
            # 没有凭证就别起了 —— 起了也会在第一次调用时 401，
            # 不如在这里明确说清"为什么没接上"。
            logger.warning("MCP harness 已开启但 .env 里没有 HARNESS_API_KEY，跳过接入")
        else:
            specs.append(
                MCPServerSpec(
                    name="harness",
                    command=settings.mcp_harness_command,
                    args=settings.mcp_harness_args.split(),
                    env={
                        **_base_env(),
                        # ⚠ 只放 harness 自己要的键，**不放宿主环境**
                        "HARNESS_API_KEY": settings.harness_api_key,
                        # 只读的正解在服务端这个变量上，不在 PAT 上（D25 更正）：
                        # 个人 PAT 继承账号全权，创建时没有只读档。
                        "HARNESS_READ_ONLY": "true" if settings.mcp_harness_read_only else "false",
                        "HARNESS_AUTO_APPROVE_RISK": settings.mcp_harness_auto_approve_risk,
                        "npm_config_registry": settings.mcp_npm_registry,
                        "LOG_LEVEL": "info",   # 显式覆盖，不给宿主环境留污染机会
                    },
                    namespace="",   # harness 的工具**自带** `harness_` 前缀，不再叠加
                )
            )

    return specs
