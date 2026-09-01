"""
D7 验证脚本：LLM Gateway 双通道降级
====================================
1. 主通道正常直答（DeepSeek）
2. 模拟主通道故障 → 自动切备用通道（OpenAI）→ 恢复后主通道优先
用法：uv run python -m scripts.d7_gateway_verify

说明：模拟故障的方式是临时把主通道 base_url 指向无效地址（不修改 .env），
验证后自动恢复，不影响正常使用。
"""

import asyncio

from app.config import settings
from app.core.logging import logger
from app.services.llm_gateway import chat


async def case_primary() -> None:
    """用例 1：主通道正常直答。"""
    print(f"\n{'='*50}\n用例 1：主通道正常直答\n{'='*50}")
    reply = await chat([{"role": "user", "content": "用一句话介绍你自己"}], trace_name="d7-primary")
    print(f"✅ 主通道回复: {reply[:60]}")


async def case_failover() -> None:
    """用例 2：模拟主通道故障 → 自动切备用。"""
    print(f"\n{'='*50}\n用例 2：主通道故障 → 降级到备用\n{'='*50}")
    original_url = settings.deepseek_base_url
    try:
        settings.deepseek_base_url = "http://localhost:59999"  # 无效地址，必然失败
        reply = await chat([{"role": "user", "content": "用一句话介绍你自己"}], trace_name="d7-failover")
        print(f"✅ 降级成功，回复（来自备用通道）: {reply[:60]}")
    finally:
        settings.deepseek_base_url = original_url  # 恢复
        print("ℹ️ 主通道已恢复")


async def main() -> None:
    await case_primary()
    await case_failover()
    print("\n全部用例完成。Langfuse 面板应出现 d7-primary 和 d7-failover 两条 trace。")


if __name__ == "__main__":
    asyncio.run(main())
