"""
工具：current_time
==================
D8 起改为"注册制"：工具用 @register 自己声明，不再需要修改 Agent 引擎。

对比 D6（内联 dict 方式）：
- D6：改 agent_service.py 的 TOOLS dict 才能加工具
- D8：只要这个文件加装饰器，注册表自动收录（引擎零改动）
"""

from datetime import datetime

from app.tools.registry import register


@register(
    name="current_time",
    description="获取当前日期和时间。当用户问'现在几点/今天几号/当前时间'时使用。",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
def current_time() -> str:
    """返回当前本地时间（字符串）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
