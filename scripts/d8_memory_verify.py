"""
D8 验证脚本：记忆滑窗（多轮对话上下文）
=========================================
1. 第一轮：告诉 Agent 名字 → 写入记忆
2. 第二轮：问"我叫什么" → Agent 应从记忆答出（上下文正确）
3. 检查 Redis 里的滑窗数据
4. 验证滑窗裁剪（写入超过上限，旧消息被挤掉）
用法：uv run python -m scripts.d8_memory_verify
"""

import asyncio
import uuid

from app.core.logging import logger
from app.core.redis import redis_client
from app.services.agent_service import run_agent
from app.services.memory_service import MAX_MESSAGES, _window_key, get_window


async def case_memory() -> bool:
    """用例 1：两轮对话，验证上下文记忆。"""
    # D14：session_id 改为 uuid.UUID —— run_agent 现在要求与 PG 的 uuid 列同类型
    session_id = uuid.uuid4()
    print(f"\n{'='*55}\n用例 1：多轮记忆（session={session_id}）\n{'='*55}")

    # 第一轮：告诉名字
    r1 = await run_agent(session_id, "我叫小王，请记住")
    print(f"[第 1 轮] 用户: 我叫小王，请记住")
    # D14 修复：run_agent 从 D11 起返回 ChatResult 而不是 str，
    # 这里一直写着 r1[:60]（当字符串用）→ 一跑到这就 TypeError。
    # 正是铁律 10「改了返回类型必须立刻改所有调用点」漏掉的那个脚本。
    print(f"[第 1 轮] Agent: {r1.answer[:60]}")

    # 第二轮：问名字（关键验证点）
    r2 = await run_agent(session_id, "我叫什么名字？")
    print(f"\n[第 2 轮] 用户: 我叫什么名字？")
    print(f"[第 2 轮] Agent: {r2.answer[:80]}")

    # 判断是否记住了
    ok = "小王" in r2.answer
    print(f"\n{'✅ 记忆生效（答出了名字）' if ok else '❌ 记忆失效（没答出名字）'}")

    # 检查 Redis 数据
    window = await get_window(session_id)
    print(f"\nRedis 滑窗内容（{len(window)} 条）:")
    for m in window:
        print(f"  {m['role']}: {m['content'][:40]}")

    # 清理测试数据
    await redis_client.delete(_window_key(session_id))
    return ok


async def case_window_trim() -> bool:
    """用例 2：滑窗裁剪（写入超过上限，验证旧的被挤掉）。"""
    # 这个用例只碰 Redis（append_turn / get_window），不进 PG，所以仍用普通字符串 id
    session_id = f"trim-{uuid.uuid4().hex[:8]}"
    print(f"\n{'='*55}\n用例 2：滑窗裁剪（上限 {MAX_MESSAGES} 条消息）\n{'='*55}")

    # 直接往记忆里写 12 轮（不调 LLM，省时间）
    from app.services.memory_service import append_turn

    for i in range(1, 13):
        await append_turn(session_id, f"用户消息{i}", f"回复{i}")

    window = await get_window(session_id)
    print(f"写入 12 轮（24 条）后，窗口实际保留: {len(window)} 条（上限 {MAX_MESSAGES}）")
    print(f"最旧一条: {window[0]['content']}")
    print(f"最新一条: {window[-1]['content']}")
    ok = len(window) == MAX_MESSAGES and window[0]["content"] == "用户消息5"
    print(f"\n{'✅ 裁剪正确（1-8 条被挤掉，保留 5-12 轮）' if ok else '❌ 裁剪异常'}")

    await redis_client.delete(_window_key(session_id))
    return ok


async def main() -> None:
    results = [
        ("用例 1 多轮记忆", await case_memory()),
        ("用例 2 滑窗裁剪", await case_window_trim()),
    ]
    print("\n" + "=" * 55)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    passed = sum(1 for _, ok in results if ok)
    print(f"\n  总计 {passed}/{len(results)} 通过")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
