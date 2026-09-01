"""
工具模块：current_time
======================
D6 最小工具：返回当前时间。用于验证 ReAct 图的"工具调用路径"。

D9 会升级为 ToolRegistry（name/description/parameters/handler 四元组注册制），
这里先直接定义，D9 迁移到 Registry。
"""

from datetime import datetime


def current_time() -> str:
    """返回当前本地时间（字符串）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
