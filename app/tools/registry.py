"""
工具注册表（Tool Registry）
============================
中心化的工具管理：工具自己"声明注册"，Agent 引擎只查注册表。

设计目标（D8 解决的问题）：
- D6 时代：工具定义内联在 agent_service.py 里 → 加工具要改 Agent 引擎（耦合）
- D8 时代：工具在自己文件里用 @register 注册 → 加工具 = 新建文件 + 装饰器，引擎零改动

三个对外能力：
1. register(...)        —— 装饰器，工具用它把自己注册进来
2. get_tools_schema()   —— 生成 OpenAI function calling 的 tools schema（给 LLM 看）
3. execute_tool(...)    —— 按名字+参数执行工具（给 execute_node 用）

类比前端：webpack/Vite 插件系统——插件自己注册，构建工具只管遍历注册表。
"""

import inspect
from collections.abc import Callable
from typing import Any

# 全局注册表：{工具名: {function, description, parameters}}
# 模块级私有变量（前面加 _ 表示"外部别直接碰，用下面三个函数操作"）
_registry: dict[str, dict] = {}


def register(
    name: str,
    description: str,
    parameters: dict | None = None,
    source: str = "local",
) -> Callable:
    """
    装饰器：把一个函数注册为工具。

    用法：
        @register(name="current_time", description="获取当前时间")
        def current_time() -> str: ...

    Args:
        name: 工具名（LLM 调用时用的名字，要唯一）
        description: 工具描述（LLM 靠它判断何时调用，要写清触发场景）
        parameters: JSON Schema 格式的参数定义（无参数传空对象）
        source: 这个工具**从哪来**（D27 新增）。默认 "local" = 项目内自己写的；
                动态发现的 MCP 工具传 server 名（如 "inventory" / "harness"）。
                存在的理由：注册表现在同时装着两种出身完全不同的工具 ——
                进程内的真函数，和"函数体在另一个进程里"的代理（stub）。
                没有这个字段，`/api/tools` 与 D28 的"某次调用来自哪"就只能靠
                工具名猜（而名字是可以随便起的）；有故障时也分不清该重启谁。
    Returns:
        装饰器函数
    """

    def decorator(func: Callable) -> Callable:
        if name in _registry:
            raise ValueError(f"工具名重复：{name}（工具名必须唯一）")
        _registry[name] = {
            "function": func,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}, "required": []},
            "source": source,   # D27：记出处，供 /api/tools 与观测层使用
        }
        return func  # 原样返回，函数本身仍可正常调用

    return decorator


def get_tools_schema() -> list[dict]:
    """生成 OpenAI function calling 的 tools schema（发给 LLM）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": meta["description"],
                "parameters": meta["parameters"],
            },
        }
        for name, meta in _registry.items()
    ]


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """
    按名字执行工具，返回字符串结果。

    D10 变化：改 async，并支持「同步/异步工具共存」。
    原因：D9 的检索工具是 async def，同步注册表直接调用它只会拿到一个
    coroutine 对象，str() 之后变成 "<coroutine object ...>" 喂给 LLM——
    不报错，但结果是垃圾（Python 仅抛 RuntimeWarning，容易被忽略）。

    设计取舍：注册表不假设工具是同步还是异步，改为运行时探测返回值
    （inspect.isawaitable）。这样：
    - 同步工具（current_time）原样返回，不为统一而多造 coroutine、多一次调度
    - 异步工具（search_documents）自动 await
    - 旧工具零改动，新工具不受限

    Args:
        name: 工具名（来自 LLM 的 tool_calls）
        arguments: 参数字典（已从 JSON 解析）
    Returns:
        工具执行结果（字符串）；出错时返回错误说明而非抛异常
        （执行失败不该拖垮整个 Agent 循环，交给 LLM 决定怎么应对）
    """
    if name not in _registry:
        return f"未知工具：{name}（可用工具：{list(_registry.keys())}）"
    try:
        result = _registry[name]["function"](**arguments)
        if inspect.isawaitable(result):   # 探测：是 coroutine 就 await 出来
            result = await result
        return str(result)
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"


def list_tools() -> list[str]:
    """列出所有已注册工具名（调试/文档用）。"""
    return list(_registry.keys())


def describe_tools() -> list[dict]:
    """
    导出全部工具的**描述信息**（不含函数对象），供 `/api/tools` 与调试使用。

    与 `get_tools_schema()` 的区别（别混用）：
    - `get_tools_schema()` 是**发给 LLM** 的，格式必须严格是 OpenAI function calling
      的形状（`{"type": "function", "function": {...}}`），多一个字段都可能惹麻烦。
    - `describe_tools()` 是**发给人/前端/验收脚本**的，所以可以带上 LLM 不关心、
      但排障必需的东西 —— `source`（来自哪个 server）。

    刻意**不返回** `function` 对象：它是不可 JSON 序列化的，混进去会让这个函数
    只能在 Python 里用，接口层就没法直接调。
    """
    return [
        {
            "name": name,
            "description": meta["description"],
            "parameters": meta["parameters"],
            "source": meta.get("source", "local"),
        }
        for name, meta in _registry.items()
    ]


def unregister_source(source: str) -> list[str]:
    """
    摘掉某个来源的全部工具，返回被摘掉的名字（D27 新增）。

    为什么需要它 —— 这是 D27 原理环节 Q1-L3 那道题的正解：
    `_registry` 是个内存字典，**MCP 子进程死掉不会自动清空它**。不主动摘的话，
    `get_tools_schema()` 会继续把这批"已经调不通的工具"报给 LLM，
    LLM 就会基于一个假前提做规划、每轮白烧一次调用 —— 而且全程不报错。

    ⚠ 只摘 `source` 严格等于参数的那些，**绝不动 "local"**（那是进程内的真函数，
    与外部 server 生死无关）。

    ⚠ 摘除发生在"一次对话的两次请求之间"时是安全的；但**正在执行的
    `execute_tool` 若已取到函数对象，仍会跑完**（Python 的 dict 遍历不会因
    删除键而中断已取出的引用）。这是可接受的：那一轮会拿到一次失败文本，
    下一轮就不再有这个工具了。
    """
    doomed = [n for n, meta in _registry.items() if meta.get("source") == source]
    for name in doomed:
        _registry.pop(name, None)
    return doomed
