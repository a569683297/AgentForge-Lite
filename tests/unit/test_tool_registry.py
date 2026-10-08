"""
单元：工具注册表 `registry`（D8 注册机制，D27 加 `source`）
=============================================================
注册表是 Agent 引擎与工具之间的**唯一耦合点**：
引擎只认三件事 —— 有哪些工具、schema 长什么样、按名字怎么执行。
它一旦出错，症状会跑到很远的地方（LLM 收到垃圾字符串、或基于不存在的前提做规划）。
"""

import json

import pytest


def test_register_then_describe(registry):
    @registry.register(name="t_demo", description="演示工具")
    def _demo() -> str:
        return "ok"

    described = {t["name"]: t for t in registry.describe_tools()}
    assert described["t_demo"]["description"] == "演示工具"
    assert described["t_demo"]["source"] == "local"  # 默认出身：项目内自己写的


def test_default_parameters_is_empty_object_schema(registry):
    """没给 parameters 时要给一个**合法的空 schema**，不能是 None ——
    schema 会被原样发给 LLM，None 会让整个请求格式非法。"""

    @registry.register(name="t_noparam", description="无参工具")
    def _noparam() -> str:
        return "ok"

    params = {t["name"]: t for t in registry.describe_tools()}["t_noparam"]["parameters"]
    assert params == {"type": "object", "properties": {}, "required": []}


def test_duplicate_name_is_rejected_at_registration(registry):
    """重名必须在**注册时**就炸（启动就发现），不能等调用时才发现。"""

    @registry.register(name="t_dup", description="第一个")
    def _first() -> str:
        return "first"

    with pytest.raises(ValueError, match="重复"):
        registry.register(name="t_dup", description="第二个")(lambda: "second")


def test_tools_schema_matches_openai_function_calling_shape(registry):
    """
    发给 LLM 的 schema 形状是**协议约定**，多一个字段都可能惹麻烦 ——
    所以这里连顶层键集合都钉住。
    """

    @registry.register(
        name="t_schema",
        description="带参数的工具",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    )
    def _schema(q: str) -> str:
        return q

    entry = next(s for s in registry.get_tools_schema() if s["function"]["name"] == "t_schema")
    assert entry["type"] == "function"
    assert set(entry["function"]) == {"name", "description", "parameters"}


def test_describe_tools_is_json_serializable(registry):
    """
    ★ `describe_tools()` 刻意**不返回** `function` 对象（它不可 JSON 序列化）。
    这条断言就是那个设计的证据：序列化得通，说明没混进函数对象。
    """

    @registry.register(name="t_json", description="d")
    def _json() -> str:
        return "ok"

    json.dumps(registry.describe_tools())  # 不抛即通过


# ------------------------------------------------------------------ 执行


async def test_execute_unknown_tool_returns_text_not_exception(registry):
    """未知工具要变成**文本**回给 LLM（让它能改口），而不是抛异常把整轮对话打断。"""
    out = await registry.execute_tool("根本没有这个工具", {})
    assert "未知工具" in out


async def test_execute_sync_tool(registry):
    @registry.register(name="t_sync", description="d")
    def _sync() -> str:
        return "sync-ok"

    assert await registry.execute_tool("t_sync", {}) == "sync-ok"


async def test_execute_async_tool(registry):
    """
    ★ 同步/异步工具必须共存。
    D10 踩过的坑：注册表若不 await，异步工具会返回 `<coroutine object ...>` ——
    Python 只发个 RuntimeWarning，不报错，于是 LLM 收到一坨垃圾。
    """

    @registry.register(name="t_async", description="d")
    async def _async() -> str:
        return "async-ok"

    assert await registry.execute_tool("t_async", {}) == "async-ok"


async def test_execute_passes_arguments(registry):
    @registry.register(name="t_args", description="d")
    def _args(a: int, b: str = "x") -> str:
        return f"{a}-{b}"

    assert await registry.execute_tool("t_args", {"a": 1, "b": "y"}) == "1-y"


async def test_tool_exception_becomes_text(registry):
    """工具内部抛错**不该**拖垮 Agent 循环 —— 变成文本，交给 LLM 决定怎么应对。"""

    @registry.register(name="t_boom", description="d")
    def _boom() -> str:
        raise RuntimeError("工具内部炸了")

    out = await registry.execute_tool("t_boom", {})
    assert "执行失败" in out and "工具内部炸了" in out


async def test_result_is_always_string(registry):
    """注册表的对外契约是"一定返回字符串"（非字符串要 str() 掉）。"""

    @registry.register(name="t_dict", description="d")
    def _dict() -> dict:
        return {"k": 1}

    out = await registry.execute_tool("t_dict", {})
    assert isinstance(out, str)


# ------------------------------------------------------------------ source 字段（D27）


def test_source_is_recorded_and_queryable(registry):
    @registry.register(name="t_mcp", description="d", source="inventory")
    def _mcp() -> str:
        return "ok"

    assert registry.get_tool_source("t_mcp") == "inventory"


def test_unknown_tool_source_is_unknown_not_local(registry):
    """
    ★ 未知工具必须返回 `"unknown"`，**不是** `"local"`。

    调用方拿这个值去填 Langfuse span 的 metadata。把"没有这个工具"谎报成"本地工具"，
    会在观测平台上留下一条**看起来正常的假调用记录**；而真实情况是
    "LLM 编了一个不存在的工具名"。排障时这两者必须能区分。
    """
    assert registry.get_tool_source("这个工具不存在") == "unknown"


def test_unregister_source_removes_only_that_source(registry):
    """
    ★ MCP 子进程死掉时，它在注册表里的工具**不会自动消失** ——
    必须主动摘，否则 `get_tools_schema()` 会继续把这批调不通的工具报给 LLM，
    LLM 基于假前提做规划、每轮白烧一次调用，而且**全程不报错**。

    同时这条钉住另一半：摘除**绝不能碰到 `local`**（那是进程内的真函数，
    与外部 server 生死无关），也别动别的 server。
    """

    @registry.register(name="t_keep_local", description="d", source="local")
    def _keep_local() -> str:
        return "local"

    @registry.register(name="t_drop_a", description="d", source="inventory")
    def _drop_a() -> str:
        return "a"

    @registry.register(name="t_keep_b", description="d", source="harness")
    def _keep_b() -> str:
        return "b"

    removed = registry.unregister_source("inventory")

    assert removed == ["t_drop_a"]
    assert "t_drop_a" not in registry.list_tools()
    assert "t_keep_local" in registry.list_tools()
    assert "t_keep_b" in registry.list_tools()


def test_unregister_unknown_source_is_a_noop(registry):
    """摘一个不存在的来源不该报错（比如那台 server 本来就没连上）。"""
    assert registry.unregister_source("never-connected") == []
