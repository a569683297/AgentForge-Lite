"""
工具包统一入口
==============
**必须 import 所有工具模块**，让它们的 @register 装饰器执行、把工具登记进注册表。

为什么重要（和 D2 的 models/__init__.py 同一个道理）：
- 装饰器只在"模块被导入时"执行
- 如果没人 import current_time.py，@register 就没跑，注册表就是空的
- Agent 引擎拿到空的 tools schema → LLM 完全不知道有工具可用

所以：新增工具文件后，**必须在这里 import 一次**。
"""

from app.tools import current_time  # noqa: F401  导入即注册（副作用导入）
from app.tools import retrieval  # noqa: F401  D10：知识库检索工具
from app.tools.registry import (
    execute_tool,
    get_tools_schema,
    list_tools,
    register,
)

__all__ = [
    "register",
    "get_tools_schema",
    "execute_tool",
    "list_tools",
    "current_time",
    "retrieval",
]
