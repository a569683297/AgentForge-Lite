"""
D15 验证：PG 兜底回填热窗口（补 PRD F5.3 的后半句）
====================================================
跑法（项目根目录）：uv run python -m scripts.d15_backfill_verify

三段各管一件事：
    A 数据层   —— get_window 的兜底分支本身对不对（不碰 LLM，秒级）
    B 对照实验 —— 「回填」与「只读不写回」的实测差异
    C 端到端   —— Redis 丢了之后上下文真的还能恢复吗（真跑两轮对话，会调 LLM）

C 段才是这段补账的真正验收点：D15 之前，清掉 Redis 就等于历史永久消失。

B 段为什么存在（不是凑数）：
讲解阶段我给出过一个论证 ——「兜底只读不写回 → append_turn 会把 Redis 写成一个
残缺但非空的窗口 → 下一轮命中残缺窗口 → 兜底再也不触发 → 历史静默丢失」。
写代码时重读 append_turn 发现它**内部复用的就是 get_window**（memory_service.py:144），
于是那个论证的前提可能是错的。B 段把这个分歧做成对照实验，用实测代替争论。

断言设计原则（铁律 9）：每条断言都要能回答"什么情况下它会假通过"。
本脚本统一采用「先确认前置状态 → 再断言变化」的写法（例如断言"回填生效"之前，
先确认 Redis 里确实没有 key），避免空数据恒满足。
"""

import asyncio
import json
import uuid

from sqlalchemy import delete, func, select

from app.core.db import async_session_factory, engine
from app.core.redis import redis_client
from app.models.message import Message
from app.models.session import Session
from app.services import memory_service
from app.services.memory_service import MAX_MESSAGES, WINDOW_TURNS, _window_key, get_window
from app.services.session_service import append_messages, get_recent_dialogue

# 三个固定测试会话：每次跑都用同一批 id，避免反复堆积垃圾数据
SID_MAIN = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.backfill")          # 常规用例（含 tool 行）
SID_LONG = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.backfill.long")     # 超窗用例（20 轮）
SID_EMPTY = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.backfill.empty")   # 全新会话（PG 无记录）
NON_UUID = "d15-not-a-uuid"                                                   # 非 uuid 形态的 id

# 一轮「完整」的落库消息：user → assistant(带 tool_calls) → tool → assistant(最终回答)
# 这四种形态齐全，才能验证兜底的过滤规则
MAIN_TURN_1 = [
    {"role": "user", "content": "「琥珀」项目谁负责？"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "search_knowledge", "arguments": '{"query": "琥珀"}'}}
    ]},
    {"role": "tool", "content": "琥珀项目由星河组负责 [1]"},
    {"role": "assistant", "content": "琥珀项目由星河组负责 [1]"},
]
MAIN_TURN_2 = [
    {"role": "user", "content": "那「翡翠」呢？"},
    {"role": "assistant", "content": "翡翠项目由南山组负责 [1]"},
]
# 期望回填的结果：只留「对话语义」——tool 行与带 tool_calls 的决策行都被排掉
MAIN_EXPECTED = ["「琥珀」项目谁负责？", "琥珀项目由星河组负责 [1]",
                 "那「翡翠」呢？", "翡翠项目由南山组负责 [1]"]

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果（失败不中断，最后统一汇总）。"""
    (passed if ok else failed).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{('  —— ' + detail) if detail else ''}")


async def drop_session(sid: uuid.UUID) -> None:
    """删除会话（连带删消息，靠 DB 的 ON DELETE CASCADE）。"""
    async with async_session_factory() as db:
        await db.execute(delete(Session).where(Session.id == sid))
        await db.commit()


async def redis_get(sid: uuid.UUID | str) -> str | None:
    """直连 Redis 看某个热窗口 key 的原始值（不信函数的返回值）。"""
    return await redis_client.get(_window_key(sid))


async def redis_del(sid: uuid.UUID | str) -> None:
    await redis_client.delete(_window_key(sid))


async def count_messages(sid: uuid.UUID) -> int:
    """查该会话在 messages 表里的实际行数。"""
    async with async_session_factory() as db:
        return (
            await db.execute(
                select(func.count()).select_from(Message).where(Message.session_id == sid)
            )
        ).scalar_one()


# ============================================================
# A. 数据层：兜底分支本身对不对
# ============================================================
async def case_a() -> None:
    print("\n=== A. 数据层：get_window 的兜底分支 ===\n")

    # ---------- 前置清理：三个会话从干净状态开始 ----------
    await drop_session(SID_MAIN)
    await drop_session(SID_LONG)
    await drop_session(SID_EMPTY)
    await redis_del(SID_MAIN)
    await redis_del(SID_LONG)
    await redis_del(SID_EMPTY)
    await redis_del(NON_UUID)

    # 造数据：MAIN 会话 6 条（含 tool 行），LONG 会话 40 条（20 轮）
    await append_messages(SID_MAIN, MAIN_TURN_1 + MAIN_TURN_2, title_hint="「琥珀」项目谁负责？")
    long_messages = []
    for i in range(1, 21):
        long_messages.append({"role": "user", "content": f"第{i}问"})
        long_messages.append({"role": "assistant", "content": f"第{i}答"})
    await append_messages(SID_LONG, long_messages, title_hint="第1问")
    check(
        "前置：PG 数据已就位（6 条 / 40 条）",
        await count_messages(SID_MAIN) == 6 and await count_messages(SID_LONG) == 40,
    )
    check(
        "前置：三个会话的 Redis 热窗口均为空",
        await redis_get(SID_MAIN) is None
        and await redis_get(SID_LONG) is None
        and await redis_get(SID_EMPTY) is None,
    )

    # ---------- A1：Redis 未命中 → 从 PG 回填 ----------
    window = await get_window(SID_MAIN)
    check("A1 未命中时返回了历史（非空）", len(window) > 0, f"{len(window)} 条")
    check(
        "A1 内容与期望一致（顺序也对）",
        [m["content"] for m in window] == MAIN_EXPECTED,
        f"实际 {[m['content'] for m in window]}",
    )

    # ---------- A2：回填是「写」操作，不只是「读」----------
    raw = await redis_get(SID_MAIN)
    check("A2 Redis 里真的写进去了", raw is not None)
    check(
        "A2 写进去的内容与返回值一致",
        raw is not None and json.loads(raw) == window,
    )

    # ---------- A3：过滤规则（tool 行 / 决策行都不能进窗口）----------
    check("A3 窗口里没有 role='tool' 的行", all(m["role"] != "tool" for m in window))
    check(
        "A3 窗口里没有「带 tool_calls 的决策行」",
        all(not m.get("tool_calls") for m in window),
    )
    check(
        "A3 工具结果里的编号 [1] 保留住了（语义没丢）",
        any("[1]" in m["content"] for m in window),
    )

    # ---------- A4：顺序（时间正序，问在前答在后）----------
    check(
        "A4 role 顺序是 user→assistant 交替",
        [m["role"] for m in window] == ["user", "assistant", "user", "assistant"],
        f"实际 {[m['role'] for m in window]}",
    )

    # ---------- A5：超窗裁剪（PG 里有 20 轮，只该回填最近 8 轮）----------
    long_window = await get_window(SID_LONG)
    check(
        "A5 只回填最近 N 轮（16 条，不是 40 条）",
        len(long_window) == MAX_MESSAGES,
        f"{len(long_window)} 条 / 期望 {MAX_MESSAGES}",
    )
    check(
        "A5 取的是「最近」那段（第13问 → 第20答）",
        long_window[0]["content"] == "第13问" and long_window[-1]["content"] == "第20答",
        f"首={long_window[0]['content']!r} 尾={long_window[-1]['content']!r}",
    )

    # ---------- A6：非 uuid 的 id → 跳过兜底，且不写脏 key ----------
    result = await get_window(NON_UUID)
    check("A6 非 uuid id 返回空且不抛异常", result == [])
    check("A6 也没有往 Redis 写空窗口", await redis_get(NON_UUID) is None)

    # ---------- A7：全新会话（PG 无记录）→ 不写空数组 ----------
    check(
        "A7 前置：PG 里确实没有这个会话",
        await count_messages(SID_EMPTY) == 0,
    )
    empty_result = await get_window(SID_EMPTY)
    check("A7 全新会话返回空", empty_result == [])
    check(
        "A7 且不往 Redis 写空数组（避免把「此刻没有」固化成窗口）",
        await redis_get(SID_EMPTY) is None,
    )

    # ---------- A8：数据损坏 → 走兜底重建（而不是当作无历史）----------
    await redis_client.set(_window_key(SID_MAIN), "{this is not json")
    check("A8 前置：Redis 里是一段坏 JSON", await redis_get(SID_MAIN) == "{this is not json")
    rebuilt = await get_window(SID_MAIN)
    check(
        "A8 损坏时从 PG 重建（而非返回空）",
        len(rebuilt) == len(MAIN_EXPECTED),
        f"{len(rebuilt)} 条",
    )


# ============================================================
# B. 对照实验：回填 vs 只读不写回
# ============================================================
async def _read_only_no_writeback(session_id):
    """
    「只读不写回」版的兜底：行为与 _rebuild_from_pg 完全相同，唯独不写 Redis。
    用它替换掉真实现，就能观察到「没有回填」时的完整行为。
    """
    sid = memory_service._to_uuid(session_id)
    if sid is None:
        return []
    return await get_recent_dialogue(sid, turns=WINDOW_TURNS)


async def case_b() -> None:
    print("\n=== B. 对照实验：回填 与 只读不写回 ===\n")

    # ---------- B1：有回填 —— 一次 get_window 就让 Redis 恢复正常 ----------
    await redis_del(SID_LONG)
    check("B1 前置：Redis 已清空", await redis_get(SID_LONG) is None)
    await get_window(SID_LONG)
    check(
        "B1 有回填：一次读就让 Redis 恢复到完整窗口",
        await redis_get(SID_LONG) is not None,
        f"窗口 {'已重建' if await redis_get(SID_LONG) else '仍为空'}",
    )

    # ---------- B2：无回填 —— append_turn 会不会写出「残缺窗口」？----------
    original = memory_service._rebuild_from_pg
    memory_service._rebuild_from_pg = _read_only_no_writeback
    try:
        await redis_del(SID_LONG)
        check("B2 前置：Redis 已清空", await redis_get(SID_LONG) is None)

        read_once = await get_window(SID_LONG)
        check(
            "B2 无回填：读到了历史但 Redis 仍是空的",
            len(read_once) == MAX_MESSAGES and await redis_get(SID_LONG) is None,
            f"读到 {len(read_once)} 条 / Redis 仍空={await redis_get(SID_LONG) is None}",
        )

        # 关键观察点：append_turn 内部会重新读一次窗口
        await memory_service.append_turn(SID_LONG, "B段新问", "B段新答")
        after = json.loads(await redis_get(SID_LONG))
        # 若"残缺窗口"论证成立，这里应该是 2 条；实际应为完整的 16 条
        check(
            "B2 无回填下 append_turn 写回的仍是完整窗口（不是残缺 2 条）",
            len(after) == MAX_MESSAGES,
            f"写回 {len(after)} 条",
        )
        check(
            "B2 且最新一轮确实在最末（本轮没被丢）",
            after[-2]["content"] == "B段新问" and after[-1]["content"] == "B段新答",
        )
    finally:
        memory_service._rebuild_from_pg = original


# ============================================================
# C. 端到端：Redis 丢了，上下文还能不能恢复（真跑 LLM）
# ============================================================
async def case_c() -> None:
    from app.services.agent_service import run_agent

    print("\n=== C. 端到端：清空 Redis 后跨轮恢复（会调 LLM）===\n")

    sid = uuid.uuid4()
    await redis_del(sid)

    # 第 1 轮：植入一个只存在于对话里的事实
    r1 = await run_agent(sid, "请记住一个暗号：「蓝鲸」。只回答收到。")
    check("C1 第 1 轮正常返回", bool(r1.answer))
    window1 = json.loads(await redis_get(sid))
    check("C1 前置：Redis 热窗口有 2 条", len(window1) == 2, f"{len(window1)} 条")

    # 模拟 Redis 过期 / 重启：只清热窗口，PG 不动
    await redis_del(sid)
    check("C2 前置：热窗口已清空（PG 未动）", await redis_get(sid) is None)
    check("C2 前置：PG 里历史仍在", await count_messages(sid) == 2)

    # 第 2 轮：问第 1 轮的事实 —— 只有从 PG 回填才答得出来
    r2 = await run_agent(sid, "我刚才让你记的暗号是什么？")
    check(
        "C3 跨越 Redis 丢失恢复了上下文（答出暗号）",
        "蓝鲸" in r2.answer,
        f"回答={r2.answer[:40]!r}",
    )

    window2 = json.loads(await redis_get(sid))
    check(
        "C4 热窗口被重建为 4 条（2 轮 × 2）",
        len(window2) == 4,
        f"{len(window2)} 条",
    )
    check(
        "C4 重建后的窗口按时间正序、且不含工具行",
        window2[0]["role"] == "user" and all(m["role"] in ("user", "assistant") for m in window2),
    )

    # 收尾：清掉这个端到端会话，不给下次留残留
    await drop_session(sid)
    await redis_del(sid)


# ============================================================
# 主流程
# ============================================================
async def main() -> None:
    print("\n=== D15 验证：PG 兜底回填热窗口 ===\n")
    try:
        await case_a()
        await case_b()
        await case_c()
    finally:
        # 清理 A/B 的测试数据
        for sid in (SID_MAIN, SID_LONG, SID_EMPTY):
            await drop_session(sid)
            await redis_del(sid)
        await redis_del(NON_UUID)
        # 同一个 loop 内关掉连接（跨 loop 关会抛 Event loop is closed）
        await engine.dispose()
        await redis_client.aclose()

    print(f"\n=== 结果：{len(passed)} 通过 / {len(failed)} 失败 ===")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        raise SystemExit(1)
    print("全部通过 ✅\n")


if __name__ == "__main__":
    asyncio.run(main())
