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
