"""
D5 验证脚本：真实调用 DeepSeek 一次，验证 Langfuse 追踪生效
用法：uv run python -m scripts.d5_langfuse_verify
"""

import asyncio

from app.core.logging import logger
from app.services.llm_gateway import chat


async def main() -> None:
    messages = [
        {"role": "system", "content": "你是一个简洁的 AI 助手，用一句话回答。"},
        {"role": "user", "content": "用一句话介绍你自己"},
    ]
    reply = await chat(messages, trace_name="d5-verify")
    logger.info("收到回复: %s", reply)
    print("\n✅ 调用成功！现在去 Langfuse 面板查看：")
    print("   打开 https://cloud.langfuse.com → 你的项目 → Traces")
    print("   应能看到名为 'd5-verify' 的 trace，展开可见 llm-call span 和 token 计数")


if __name__ == "__main__":
    asyncio.run(main())
