"""
集成：MCP 客户端生命周期与崩溃容错（D27）
==========================================
PRD §14 把这一条明确列进集成层，理由很硬：**外部 server 是本应用控制不了的东西**
（要网络、要 npm、要 PAT）。它挂掉时应用必须还能用 —— 这个"必须"只能靠测试证明。

这里的"外部 server"用的是**本机自研的 `mcp_servers/inventory_server.py`**：
它是真协议、真子进程，但不碰网络、不要 PAT。
PRD §14 要的"避免 CI 依赖外部网络与凭证"正是这个意思。

⚠ 真用 Harness SaaS 的用例在 `test_external_harness.py`，标 `external` 默认跳过。
"""

import sys
from pathlib import Path

import pytest

from app.mcp.client import MCPClient, MCPServerSpec
from app.tools.registry import unregister_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_SERVER = PROJECT_ROOT / "mcp_servers" / "inventory_server.py"

# 测试专用的命名空间（前缀），避免和真接入的 "inventory" 撞名导致注册表重名报错。
TEST_NAMESPACE = "mcptest"


def _spec(namespace: str = TEST_NAMESPACE, **overrides) -> MCPServerSpec:
    base = {
        "name": "mcptest-server",
        "command": sys.executable,          # 当前 venv 的 python（绝对路径，不依赖 PATH）
        "args": [str(INVENTORY_SERVER)],
        "cwd": str(PROJECT_ROOT),
        "namespace": namespace,
        "call_timeout_s": 15.0,
    }
    base.update(overrides)
    return MCPServerSpec(**base)


@pytest.fixture
async def connected_client():
    """
    真起一个 MCP 子进程并完成握手；用例结束**保证**回收（含注册表清理）。

    为什么清理要写在 finally 里：注册表是全局的，"上一个用例的残留"
    会让下一个用例以"工具名重复"的方式莫名其妙地红。
    """
    client = MCPClient(_spec())
    await client.connect(timeout_s=20)
    try:
        yield client
    finally:
        await client.close()
        # ⚠ 摘除用的是 **spec.name**（注册时写进 `source` 的正是它），**不是 namespace**。
        #   两者在本项目里是两个概念：namespace 只影响工具**名**的前缀，
        #   source 记的是"这工具的主人是谁"。写错就会留下摘不掉的工具 ——
        #   下一个用例会以"工具名重复"的方式红，而原因看起来完全无关。
        unregister_source(client.spec.name)


# ============================================================
# 一、纯逻辑（不起进程）
# ============================================================


def test_namespace_prefix_is_applied():
    """前缀是**显式配置**，不是猜的 —— 两台 server 都可能有 `get`/`list` 这类泛名。"""
    client = MCPClient(_spec(namespace="inv"))
    assert client._qualify("query_inventory") == "inv_query_inventory"


def test_empty_namespace_means_no_prefix():
    """Harness 的工具名**自带**前缀，再加一层会变成 `harness_harness_list`。"""
    client = MCPClient(_spec(namespace=""))
    assert client._qualify("harness_list") == "harness_list"


async def test_call_without_connection_returns_text_not_exception():
    """
    未连接时调用必须返回**文本**（继承 D10 的注册表契约），不能抛。
    抛出去会被 `run_agent` 的通用 except 变成 500，用户这一轮直接白跑。
    """
    client = MCPClient(_spec())
    out = await client.call("whatever", {})
    assert isinstance(out, str)
    assert "未连接" in out


# ============================================================
# 二、结果渲染（`_render` 的四种形态）
# ============================================================


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, content=None, is_error=False, structured_content=None) -> None:
        self.content = content or []
        self.is_error = is_error
        self.structured_content = structured_content


def test_render_joins_content_blocks():
    """取 `content`（给模型读的那一层），多个 block 用换行拼起来。"""
    client = MCPClient(_spec())
    out = client._render("t", _Result(content=[_Block("第一行"), _Block("第二行")]))
    assert out == "第一行\n第二行"


def test_render_error_result_still_returns_text():
    """`is_error=True` 也**照样返回文本**（前面加一句"返回错误"），不在这里再抛一遍。"""
    client = MCPClient(_spec())
    out = client._render("t", _Result(content=[_Block("权限不足")], is_error=True))
    assert "返回错误" in out and "权限不足" in out


def test_render_falls_back_to_structured_content():
    """没有文本块时退回 `structuredContent`（它是给程序用的那一视角）。"""
    client = MCPClient(_spec())
    out = client._render("t", _Result(structured_content={"total": 1}))
    assert "total" in out


def test_render_empty_result_is_not_empty_string():
    """空结果要有明确文案 —— 空字符串会让 LLM 分不清"没数据"和"调用坏了"。"""
    client = MCPClient(_spec())
    assert client._render("t", _Result()) == "(工具返回空结果)"


# ============================================================
# 三、真起子进程（生命周期）
# ============================================================


async def test_connect_captures_handshake(connected_client):
    """握手情报（server 名 / 协议版本）是排障时最先要看的两样，必须留下来。"""
    assert connected_client.server_info is not None
    assert getattr(connected_client.server_info, "name", None) == "inventory"
    assert connected_client.protocol_version  # 非空字符串


async def test_discover_and_register_uses_server_declared_names(connected_client):
    """
    ★ 零硬编码：工具名、描述、schema 全部来自 `tools/list`。
    这里断言的是"注册进来的名字 == server 自报的名字 + 配置的前缀"，
    而不是断言某个写死的名字。
    """
    registered = await connected_client.discover_and_register()

    assert registered, "一个工具都没注册进来"
    assert all(name.startswith(f"{TEST_NAMESPACE}_") for name in registered)

    from app.tools.registry import describe_tools

    by_name = {t["name"]: t for t in describe_tools()}
    for name in registered:
        assert by_name[name]["source"] == "mcptest-server"
        assert by_name[name]["description"]  # 描述确实透传了


async def test_real_call_returns_data(connected_client):
    """真调一次：跨两条进程边界（写 stdin → 读 stdout）拿回数据。"""
    await connected_client.discover_and_register()
    out = await connected_client.call("query_inventory", {"min_stock": 1000})

    assert isinstance(out, str)
    assert "sku" in out, f"返回里没有数据字段：{out[:200]}"


async def test_call_unknown_tool_returns_text_not_exception(connected_client):
    """server 侧报错也要变成文本（LLM 看得见，能自己改口）。"""
    out = await connected_client.call("这个工具不存在", {})
    assert isinstance(out, str)
    assert "执行失败" in out or "错误" in out


async def test_close_does_not_remove_tools_but_manager_does(connected_client):
    """
    ★ 一个**反直觉但真实**的行为，也是 D27 原理环节 Q1-L3 的答案：

    `MCPClient.close()` **不会**把工具从注册表摘掉 —— 摘除是
    `MCPManager.stop()` 的职责（它才调 `unregister_source`）。

    为什么必须把这件事写进测试：client 被单独使用、或进程被 kill 时，
    注册表里会留下**已经调不通的工具**。而 `get_tools_schema()` 会继续把它们
    报给 LLM → LLM 基于假前提做规划、每轮白烧一次调用 —— **全程不报错**。

    本用例同时钉住两个事实：
      ① 断开后那些名字**还在**注册表里（所以"清理"必须是显式的，不能指望 close）
      ② 但调用它们只会拿到失败文本（不会抛异常把整轮对话打断）
    """
    from app.tools.registry import list_tools

    registered = await connected_client.discover_and_register()
    assert set(registered) <= set(list_tools())

    await connected_client.close()

    still_there = set(registered) & set(list_tools())
    assert still_there, "前提变了：close() 现在会自动摘工具了吗？（那测试得改）"

    # ② 摘不掉的后果 —— 调不通，但契约仍是"返回字符串"
    from app.tools.registry import execute_tool

    out = await execute_tool(sorted(still_there)[0], {})
    assert isinstance(out, str)
    assert "未连接" in out or "执行失败" in out, f"没有报错反而像成功了：{out[:200]}"


async def test_manager_stop_removes_tools(connected_client):
    """
    真正负责清理的是 manager：`unregister_source(spec.name)` 之后工具必须消失。

    ⚠ 这里直接调 `unregister_source` 而不是 `MCPManager.stop()`：
      manager 是**进程级单例**，测试里去起它会牵动真实的全局状态（还会连带
      起 harness 子进程）。这里要验的是"摘除这件事做得对不对"，
      而 `stop()` 调的就是这一个函数。
    """
    from app.tools.registry import list_tools

    registered = await connected_client.discover_and_register()

    removed = unregister_source(connected_client.spec.name)

    assert set(removed) == set(registered)
    assert not (set(registered) & set(list_tools()))


async def test_connect_failure_raises_and_leaves_no_half_state():
    """
    起不来时必须抛错、且**不留半截**（子进程 + 文件句柄）。
    用一台不存在的命令来制造失败 —— 这同时验证了降级链的前提：
    失败是**可捕获**的，所以上层才能把它收敛成 status 而不拖死启动。
    """
    client = MCPClient(_spec(name="never-starts", command="definitely-not-a-real-command"))
    with pytest.raises(Exception):
        await client.connect(timeout_s=10)
    assert client._stack is None
    assert client._client is None
