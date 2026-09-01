"""
D6 验证脚本：ReAct 图三路径
============================
1. 直答路径：普通问题（LLM 不调工具，直接回答）
2. 工具路径：问时间（LLM 决策调 current_time → 观察 → 回答）
3. 步骤上限：循环控制（递归限制不炸）
用法：uv run python -m scripts.d6_agent_verify
"""

import asyncio
import json

from app.core.logging import logger
from app.services.agent_service import get_agent


async def run_case(name: str, user_input: str) -> None:
    print(f"\n{'='*50}\n用例 {name}: {user_input}\n{'='*50}")
    agent = get_agent()
    state: dict = {"messages": [{"role": "user", "content": user_input}], "step_count": 0}
    result = await agent.ainvoke(state)
    # 取最后一条 assistant 消息的 content（工具调用的 assistant 可能 content 为空）
    final = ""
    for m in reversed(result["messages"]):
        if m["role"] == "assistant" and m.get("content"):
            final = m["content"]
            break
    print(f"最终回答: {final}")
    print(f"消息条数: {len(result['messages'])}")
    # 打印工具调用轨迹（如果有）
    for m in result["messages"]:
        if m["role"] == "tool":
            print(f"  [工具结果] {m['content'][:60]}")


async def main() -> None:
    await run_case("直答", "你好，请介绍一下你自己")
    await run_case("工具", "现在几点了？")


if __name__ == "__main__":
    asyncio.run(main())
