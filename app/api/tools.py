"""
工具清单接口（D27）
===================
GET /api/tools —— 列出**全部**已注册工具，标明各自来自哪里。

为什么需要它（PRD §13.2 的 D27 验收②就是它）：
    接入外部 server 之后，注册表里同时住着两种出身完全不同的工具 ——
    "进程内的真函数"与"函数体在另一个进程里的代理"。**从 LLM 那一侧看，
    这两者毫无区别**（这正是路径 B 能零改动接入的原因）。
    但人需要能分辨：出故障时要知道该重启谁、压测时要知道瓶颈在哪。

**它是"两条注册路径合流"的唯一可观察出口**：
    无论走静态 `@register` 还是走 MCP 动态发现，最终都会出现在这一个列表里。
    所以这个接口同时也是 D27 的验收工具 —— 看到 11 个 harness 工具 = 动态路径通了。

⚠ 本文件**不写任何工具名**，全部来自注册表现取。
"""

from fastapi import APIRouter

from app.mcp.manager import get_manager
from app.tools.registry import describe_tools

router = APIRouter(tags=["tools"])


@router.get("/tools")
async def list_tools() -> dict:
    """
    返回：
    {
      "total": 13,
      "by_source": {"local": ["current_time", "search_documents"], "inventory": [...], "harness": [...]},
      "servers": [{"name": "inventory", "connected": true, "tool_count": 1, ...}, ...],
      "tools": [{"name": ..., "description": ..., "parameters": {...}, "source": ...}, ...]
    }

    `servers` 单独一块，是因为它回答的是**另一个问题**：
    `tools` 说"现在有哪些工具"，`servers` 说"这些工具的主人现在什么情况"。
    两者可以不一致 —— 而那种不一致本身就是要看见的信息
    （server 断了但工具还在注册表里 = 会调不通，D27 原理环节 Q1-L3 讲的正是它）。
    """
    tools = describe_tools()

    by_source: dict[str, list[str]] = {}
    for t in tools:
        by_source.setdefault(t["source"], []).append(t["name"])

    return {
        "total": len(tools),
        "by_source": by_source,
        "servers": get_manager().status(),
        "tools": tools,
    }
