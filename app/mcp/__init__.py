"""
MCP 客户端包（D27）
====================
对外只暴露三样东西：spec（连什么）、client（怎么连）、manager（管生死）。

用法（应用启动时）：
    from app.mcp import build_specs, get_manager
    await get_manager().start(build_specs(settings))

用法（应用关闭时）：
    await get_manager().stop()
"""

from app.mcp.client import MCPClient, MCPServerSpec
from app.mcp.manager import MCPManager, build_specs, get_manager

__all__ = [
    "MCPClient",
    "MCPServerSpec",
    "MCPManager",
    "build_specs",
    "get_manager",
]
