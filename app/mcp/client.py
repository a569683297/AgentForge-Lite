"""
MCP 客户端（D27 核心）
======================
把一台**外部** MCP Server 的工具，接进本项目已有的 ToolRegistry。

为什么需要这一层（一句话版）：
    D26 我们写了 server（`mcp_servers/inventory_server.py`），它能被任何客户端调用；
    D27 我们要写**客户端** —— 让 AgentForge 成为那个"任何客户端"。
    但工具调用的耦合点只有一份公开的 JSON-RPC 报文，所以**接进来之后，
    Agent 引擎（plan / execute 两个节点）一行都不用改**。

路径 B 与路径 A 的唯一实质差别（D27 原理环节的结论）：
    - 路径 A（静态）：函数体在**本进程**，注册发生在 import 期
    - 路径 B（本文件）：函数体在**子进程**，注册发生在**运行期**
    而两者写进的是**同一张 `_registry`** —— 因为 `register` 从不检查
    "这个 callable 到底是个真函数，还是只会转发报文的代理"。

一次调用的完整往返（两条进程边界）：
    execute_node → execute_tool(name, args) → 查到 stub（在本进程）
      ── 边界 1：stub 把参数包成 JSON-RPC `tools/call` 写进子进程 stdin ──
         [子进程] 读 stdin → 查自己的表 → 校验 → 执行真函数 → 打包返回
      ── 边界 2：带同一个 id 的响应回到 stub ──
    → stub 取出文本层 → 字符串 → 以 tool 消息写回 → 回 plan 节点

⚠ 本文件**必须零硬编码工具名**：所有工具名/描述/schema 都从 `tools/list` 现取。
   写死任何一个名字，`scripts/d27_verify.py` 的"零硬编码自证"那条断言就会红。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.core.logging import logger
from app.tools.registry import register


@dataclass
class MCPServerSpec:
    """
    一台要接入的外部 MCP Server 的**启动说明书**。

    它是一份纯数据（没有行为），存在的意义是：让"连什么"与"怎么连"分开 ——
    以后加一台 server 只需要多写一份 spec，`MCPClient` 一个字都不用动。
    这正是 PRD 那条「接新 server 不改 Agent 代码」在**配置层**的形态。
    """

    name: str                                  # 这台 server 的名字（也是 source 字段的值）
    command: str                               # 可执行文件（如 npx / python）
    args: list[str] = field(default_factory=list)
    # ⚠ env 必须是**显式白名单**，不能 `{**os.environ}` 全透传：
    #   实测踩过 —— 本机 `.env` 里的 `LOG_LEVEL=INFO`（大写）透传进去后，
    #   harness server 启动瞬间 Fatal error 自杀（它只认小写枚举）。
    #   一个"看起来无关"的宿主环境变量，足以让子进程起不来。
    env: dict[str, str] = field(default_factory=dict)
    # 注册进注册表时给工具名加的前缀。空串 = 不加。
    #   - inventory：远端叫 `query_inventory` → 加前缀 → `inventory_query_inventory`
    #   - harness  ：远端**自带** `harness_` 前缀 → 传空串，避免 `harness_harness_list`
    namespace: str = ""
    cwd: str | None = None
    # 单次工具调用的超时（秒）。
    # ⚠ 它防的不是"慢"，是 **server 卡死**：子进程没了会立刻写失败（能捕获），
    #   子进程还活着但不应答则会**永远挂着** —— 而这是 `execute_tool` → `execute_node`
    #   里的一个 await，挂住一次就是挂住**整轮对话**（比报错严重得多）。
    call_timeout_s: float = 30.0


class MCPClient:
    """一台 server 的连接。负责：起进程 → 发现工具 → 造 stub → 注册 → 回收。"""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        # AsyncExitStack：让 `async with` 的上下文**跨请求常驻**。
        # 为什么要这么写 —— 官方 `Client` 是 async context manager，
        # 直接 `async with` 只能活在 with 块里；而我们要在**任意时刻**
        # （用户某轮对话决定调工具时）才用它，所以必须把"进入/退出"拆开手动控制。
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._errlog_file: Any = None
        self.stderr_path: pathlib.Path | None = None

        # 握手情报（排障时最先要看的两样）
        self.server_info: Any = None
        self.protocol_version: str | None = None
        # 本次注册进注册表的**全名**（带前缀）
        self.registered: list[str] = []

    # ---------------- 名字 ----------------
    def _qualify(self, remote_name: str) -> str:
        """
        远端工具名 → 注册到本地的全名。

        前缀规则是**显式配置**（spec.namespace），不是猜的：
        `registry.register` 重名会 `raise ValueError`（启动就炸），
        而两台 server 都可能有 `get` / `list` 这类泛名 —— 所以必须在配置层
        就把命名空间定死，不能等撞了再补。
        """
        return f"{self.spec.namespace}_{remote_name}" if self.spec.namespace else remote_name

    # ---------------- 生命周期 ----------------
    async def connect(self, timeout_s: float = 20.0) -> None:
        """
        起子进程并完成握手（含代际协商）。

        ⚠ 这一步会**阻塞到 server 就绪**。对 `npx` 类命令，首次可能拉包几分钟 ——
        所以失败必须被上层降级掉（记 failed 状态、继续启动 API），
        绝不能让一台外部 server 拖死整个应用启动。
        """
        params = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env=dict(self.spec.env),   # ← 白名单，不继承宿主环境
            cwd=self.spec.cwd,
        )

        # 子进程 stderr 落盘：它跟协议流是两根不同的管子，
        # 里面放的是**给人看的**日志。出问题时这是唯一有原文的地方。
        fd, path = tempfile.mkstemp(prefix=f"mcp-{self.spec.name}-", suffix=".stderr.log")
        self.stderr_path = pathlib.Path(path)
        self._errlog_file = open(fd, "w", encoding="utf-8", closefd=True)  # noqa: SIM115

        self._stack = AsyncExitStack()
        try:
            self._client = await asyncio.wait_for(
                self._stack.enter_async_context(
                    Client(stdio_client(params, errlog=self._errlog_file))
                ),
                timeout=timeout_s,
            )
        except Exception:
            # 起不来就把已开的东西收干净，别留下半截（子进程 + 文件句柄）
            await self._force_cleanup()
            raise

        self._capture_handshake()
        logger.info(
            "MCP 已连接 server=%s 自报=%s 版本=%s 协议=%s",
            self.spec.name,
            getattr(self.server_info, "name", None),
            getattr(self.server_info, "version", None),
            self.protocol_version,
        )

    def _capture_handshake(self) -> None:
        """
        把握手结果记下来（尽力而为，取不到不算失败）。

        为什么要记：D26 实测「默认走哪一代取决于**用哪一层客户端**」——
        高层 `Client` 默认 `mode="auto"`，先探 `server/discover`、**不发 initialize**。
        版本这件事只能在运行时读，读规范是读不出来的。
        """
        session = getattr(self._client, "session", None)
        if session is None:
            return
        self.server_info = getattr(session, "server_info", None)
        raw = getattr(session, "protocol_version", None)
        self.protocol_version = str(raw) if raw is not None else None

    async def close(self) -> None:
        """断开连接（子进程随 stdin EOF 自行退出，生命周期归宿主）。"""
        await self._force_cleanup()

    async def _force_cleanup(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as e:  # noqa: BLE001
                # 收尾阶段的异常不该外抛：此时连接已经在拆了，
                # 让一个"清理时的小毛病"把整个关闭流程打断，代价远大于收益。
                # （anyio 的 TaskGroup 会把子异常包成 ExceptionGroup，很常见。）
                logger.warning("MCP 断连时出现异常 server=%s err=%s", self.spec.name, e)
            finally:
                self._stack = None
                self._client = None
        if self._errlog_file is not None:
            try:
                self._errlog_file.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self._errlog_file = None

    # ---------------- 发现与注册 ----------------
    async def discover_and_register(self) -> list[str]:
        """
        `tools/list` → 为每个工具现造 stub → 注册进 `_registry`。

        返回注册好的**全名**列表。
        """
        if self._client is None:
            raise RuntimeError(f"MCP server「{self.spec.name}」尚未连接")

        listed = await self._client.list_tools()
        names: list[str] = []
        for tool in listed.tools:
            remote = tool.name                       # ← 名字来自 server，不是我写的
            full = self._qualify(remote)
            register(
                name=full,
                description=tool.description or "",
                # input_schema 是 server 给的 JSON Schema，**原样透传**。
                # 缺省时给一个"空对象 schema"，与 registry 内部的默认值保持一致。
                parameters=tool.input_schema
                or {"type": "object", "properties": {}, "required": []},
                source=self.spec.name,               # ← 记出处：/api/tools 靠它回答"来自哪"
            )(self._make_stub(remote, full))
            names.append(full)

        self.registered = names
        logger.info(
            "MCP 工具已注册 server=%s 数量=%d 名字=%s",
            self.spec.name,
            len(names),
            names,
        )
        return names

    def _make_stub(self, remote: str, full: str):
        """
        造一个**替身函数**（stub），代表那只住在子进程里的真工具。

        它只做三件事：① 冒充（用 server 给的描述/schema 注册，让 LLM 以为这是普通工具）
        ② 打包（把参数变成一条 `tools/call` 报文送进子进程 stdin）
        ③ 等回（在 stdout 上等**同一个 id** 的响应，取出文本层）。

        ⚠ 必须是 `async def`：`execute_tool` 里有一句 `inspect.isawaitable(result)`，
          同步函数会被原样 `str()` 掉 —— 那样 LLM 收到的是
          `"<coroutine object ...>"` 这种垃圾（Python 只发个 RuntimeWarning，不报错）。
          D10 踩过这个坑，这里不能再犯。
        """

        async def _stub(**kwargs: Any) -> str:
            return await self.call(remote, kwargs)

        # 名字只影响排障可读性（`_registry` 用的键是完整的 full）。
        _stub.__name__ = full
        _stub.__qualname__ = f"MCPClient[{self.spec.name}].{full}"
        _stub.__doc__ = f"MCP stub → {self.spec.name}.{remote}"
        return _stub

    # ---------------- 调用 ----------------
    async def call(self, remote: str, arguments: dict[str, Any]) -> str:
        """
        执行一次远端调用，**永远返回字符串**（继承 D10 的注册表契约）。

        为什么错误也要变成文本而不是抛异常：
        调用方是 `execute_tool` → `execute_node` → 写回 tool 消息给 LLM。
        抛出去会被 `run_agent` 的通用 except 变成 500，用户这一轮直接白跑；
        变成文本则**模型看得见**，能自己改口（"这个工具暂时不可用，我换个说法"）。
        """
        if self._client is None:
            return (
                f"工具 {remote} 执行失败：MCP server「{self.spec.name}」当前未连接"
            )
        try:
            result = await asyncio.wait_for(
                self._client.call_tool(remote, arguments),
                timeout=self.spec.call_timeout_s,
            )
        except (TimeoutError, asyncio.TimeoutError):
            # ⚠ 这一支覆盖的是**最严重**的故障：子进程还活着、但永远不回答。
            #   不加超时的话，这里的 await 会一直挂着 → 整轮对话卡死（用户只看到转圈）。
            #   `wait_for` 会 cancel 掉内部那个 task —— 所以**超时之后这条连接能不能
            #   继续用，是不能想当然的**（取消可能把 SDK 内部的请求/响应配对搞乱）。
            #   这一点由 scripts/d27_verify.py 的 G 段实测，不靠推理。
            return (
                f"工具 {remote} 执行失败：MCP server「{self.spec.name}」响应超时"
                f"（超过 {self.spec.call_timeout_s:.0f}s），该 server 可能已卡死"
            )
        except Exception as e:  # noqa: BLE001
            # 这一支覆盖：子进程死了（写管道失败）等。
            return (
                f"工具 {remote} 执行失败：MCP server「{self.spec.name}」调用异常 "
                f"{type(e).__name__}: {e}"
            )
        return self._render(remote, result)

    def _render(self, remote: str, result: Any) -> str:
        """
        把 `CallToolResult` 翻成喂给 LLM 的一段文本。

        取哪一层：**`content`（给模型读）优先**，而不是 `structuredContent`。
        理由（D26 实测）：server 返回 12 条物料时，`content` 是**每条一个 block**
        （12 个），那正是 LLM 习惯的"碎片"粒度；`structuredContent` 是给程序用的。
        两者是同一批数据的两个视角，**喂模型的走 content**。

        `is_error=True` 也**照样返回文本**（前面加一句"返回错误"），
        而不是在这里再抛一遍 —— 契约是"到了调用方手上一定是字符串"。
        """
        blocks = getattr(result, "content", None) or []
        texts = [b.text for b in blocks if getattr(b, "text", None)]

        if getattr(result, "is_error", False):
            body = "\n".join(texts) if texts else "(server 未提供错误详情)"
            return f"工具 {remote} 返回错误：{body}"

        if texts:
            return "\n".join(texts)

        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)

        return "(工具返回空结果)"
