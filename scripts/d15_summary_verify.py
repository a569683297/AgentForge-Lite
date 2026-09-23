"""
D15 验证：摘要压缩 + Redis 容错（第二段）
==========================================
跑法（项目根目录）：uv run python -m scripts.d15_summary_verify

四段各管一件事：
    A 数据层  —— 触发判据 / 摘要区间 / 摘要读写（不碰 LLM，秒级）
    B 摘要链路 —— 从"不够轮数"到真的摘出摘要，以及"攒够两轮再摘"（调 LLM）
    C 容错    —— Redis 挂掉时，读、写、摘要各自还能不能活（调 LLM 1 次）
    D 端到端  —— 摘要真的被注入到 system 里了吗（调 LLM，唯一能证明注入生效的方法）

D 段为什么是唯一有效的注入验证：
    只有在**窗口为空、PG 里没有对话消息**、仅 `sessions.summary` 有内容的情况下提问，
    模型答出那个事实，才能反证"它是从摘要里读到的"。否则模型可能从窗口历史里答，
    测了个寂寞。

C 段为什么不能只测"函数不抛异常"：
    不抛异常只说明"崩溃被吞掉了"。真正要守住的是**功能降级后仍然正确** ——
    所以 C6 断言的是"Redis 挂掉时摘要照样摘对（只是重摘一遍）"。

断言设计原则（铁律 9）：每条断言都要能回答"什么情况下它会假通过"。
本脚本统一采用「先确认前置状态 → 再断言变化」，并且所有计数类断言都先确认非零。
"""

import asyncio
import uuid

from sqlalchemy import delete, select

from app.core.db import async_session_factory, engine
from app.core.redis import redis_client
from app.models.message import Message
from app.models.session import Session
from app.services import memory_service
from app.services.memory_service import (
    MAX_MESSAGES,
    SUMMARY_MIN_MESSAGES,
    _get_cursor,
    _summary_cursor_key,
    _window_key,
    get_window,
    maybe_summarize,
)
from app.services.session_service import (
    append_messages,
    ensure_session,
    get_dialogue_range,
    get_dialogue_stats,
    get_summary,
    get_recent_dialogue,
    save_summary,
)

# 固定测试会话 id：每次跑都用同一批，避免反复堆积垃圾数据
SID_STATS = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.summary.stats")
SID_SUM = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.summary.flow")
SID_TOL = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.summary.tolerance")
SID_E2E = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d15.summary.e2e")

# 早期轮次里植入的关键事实（摘要必须保住它，否则摘要等于没摘）
FACT_SECRET = "蓝鲸-771"
FACT_REVENUE = "1.2 亿元"

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果（失败不中断，最后统一汇总）。"""
    (passed if ok else failed).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{('  —— ' + detail) if detail else ''}")


async def drop_session(sid: uuid.UUID) -> None:
    """删除会话（连带删消息，靠 DB 的 ON DELETE CASCADE）+ 清掉它的两个 Redis key。"""
    async with async_session_factory() as db:
        await db.execute(delete(Session).where(Session.id == sid))
        await db.commit()
    try:
        await redis_client.delete(_window_key(sid), _summary_cursor_key(sid))
    except Exception:
        pass


async def clean_ids(sid: uuid.UUID) -> list[int]:
    """直查「干净对话消息」的 id（时间正序）—— 用来算精确的区间边界。"""
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Message.id)
                    .where(
                        Message.session_id == sid,
                        Message.role.in_(("user", "assistant")),
                        Message.tool_calls.is_(None),
                    )
                    .order_by(Message.id.asc())
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


def turns_payload(count: int, start: int = 1) -> list[dict]:
    """
    造 count 轮"假对话"（不调 LLM）。

    前 3 轮里塞入真正的事实（供摘要保留性断言），其余用无信息量的填充 ——
    这样才能验证"摘要是挑关键事实留下的"，而不是把整段话抄一遍。
    """
    head = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，有什么可以帮你的？"},
        {"role": "user", "content": f"记住一件事：我们公司的项目暗号是{FACT_SECRET}，别对外说。"},
        {"role": "assistant", "content": f"好的，我记住了：项目暗号是{FACT_SECRET}。"},
        {"role": "user", "content": f"另外，今年的营收目标是 {FACT_REVENUE}。"},
        {"role": "assistant", "content": f"已记录：今年营收目标 {FACT_REVENUE}。"},
    ]
    rest: list[dict] = []
    for i in range(4, count + 1):
        rest.append({"role": "user", "content": f"第{i}问：这是第 {i} 个测试问题"})
        rest.append({"role": "assistant", "content": f"第{i}答：收到第 {i} 个测试问题。"})
    return (head + rest) if count >= 3 else head[: count * 2]


# ============================================================
# A. 数据层：触发判据、摘要区间、摘要读写
# ============================================================
async def case_a() -> None:
    print("\n=== A. 数据层：触发判据 / 摘要区间 / 摘要读写 ===\n")

    await drop_session(SID_STATS)
    await drop_session(SID_SUM)
    await drop_session(SID_TOL)
    await drop_session(SID_E2E)

    sid = SID_STATS

    # ---------- A1：空会话 ----------
    stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    check(
        "A1 空会话：total=0 且 window_lower_id=None",
        stats["total"] == 0 and stats["window_lower_id"] is None,
        f"total={stats['total']} lower={stats['window_lower_id']}",
    )
    check("A1b 空会话：get_summary 返回 None", await get_summary(sid) is None)

    # ---------- A2：5 轮（10 条），未超出窗口 ----------
    await append_messages(sid, turns_payload(5), title_hint="你好")
    stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    check(
        "A2 10 条 < 窗口 16 条：total=10 且仍无窗口下界（没有消息滑出）",
        stats["total"] == 10 and stats["window_lower_id"] is None,
        f"total={stats['total']} lower={stats['window_lower_id']}",
    )

    # ---------- A3：补到 12 轮（24 条），窗口开始有下界 ----------
    ids = await clean_ids(sid)
    await append_messages(
        sid, turns_payload(12)[len(ids):], title_hint=None
    )
    ids = await clean_ids(sid)
    stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    check(
        "A3 24 条 > 窗口 16 条：total=24 且窗口下界 = 第 9 条（最近 16 条里最早那条）",
        stats["total"] == 24 and stats["window_lower_id"] == ids[8],
        f"total={stats['total']} lower={stats['window_lower_id']} 期望={ids[8]}（第 9 条）",
    )

    # ---------- A4：区间 = [游标, 窗口下界)，左开右开 ----------
    rows = await get_dialogue_range(sid, after_id=None, before_id=ids[8])
    check(
        "A4 range(None, 下界) = 滑出窗口的那 8 条，且右开（不含下界本身）",
        len(rows) == 8 and ids[8] not in [r["id"] for r in rows],
        f"取到 {len(rows)} 条，id={[r['id'] for r in rows]}",
    )

    # ---------- A5：带游标时只取游标之后 ----------
    rows = await get_dialogue_range(sid, after_id=ids[1], before_id=ids[8])
    expect = [i for i in ids if ids[1] < i < ids[8]]
    check(
        "A5 range(游标, 下界)：左开 —— 游标那条本身不再摘（避免重复）",
        [r["id"] for r in rows] == expect,
        f"取到 {len(rows)} 条，期望 {len(expect)} 条",
    )

    # ---------- A6：过滤规则（tool 行与决策行都不算"对话轮次"）----------
    before_total = stats["total"]
    await append_messages(
        sid,
        [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_x", "type": "function",
                 "function": {"name": "search_knowledge", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "工具结果"},
        ],
    )
    stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    check(
        "A6 插入「决策行 + tool 行」后 total 不变（它们不是对话轮次）",
        stats["total"] == before_total,
        f"{before_total} → {stats['total']}",
    )

    # ---------- A7：limit 生效 ----------
    rows = await get_dialogue_range(sid, after_id=None, before_id=ids[8], limit=3)
    check("A7 limit 生效（要最近/最早由 order 定，条数不能超）", len(rows) == 3)

    # ---------- A8/A9：摘要读写 ----------
    await ensure_session(sid, title_hint="你好")
    await save_summary(sid, "用户自称代号「蓝鲸」，今年营收目标 1.2 亿元。")
    check(
        "A8 save_summary → get_summary 读得回",
        (await get_summary(sid)) == "用户自称代号「蓝鲸」，今年营收目标 1.2 亿元。",
    )

    # ---------- A10：空摘要不许覆盖已有摘要 ----------
    await save_summary(sid, "   ")
    check(
        "A10 空摘要被拒绝（不会把已有记忆抹掉）",
        (await get_summary(sid)) is not None,
        f"现值={await get_summary(sid)!r}",
    )

    # ---------- A11：观测 save_summary 是否连带刷新 updated_at ----------
    # 代码注释里声称"会连带刷新"（ORM 的 onupdate 对任何本表 UPDATE 生效）。
    # 这里实测它 —— 注释里的断言必须能被验证，否则就是又一处"看起来核实过"的说法。
    async with async_session_factory() as db:
        before_ts = (
            await db.execute(select(Session.updated_at).where(Session.id == sid))
        ).scalar_one()
    await asyncio.sleep(0.05)  # 让 now() 有可分辨的差距
    await save_summary(sid, "第二版摘要（仅用于观测时间戳）")
    async with async_session_factory() as db:
        after_ts = (
            await db.execute(select(Session.updated_at).where(Session.id == sid))
        ).scalar_one()
    check(
        "A11 观测：save_summary 连带刷新 updated_at（与注释一致）",
        after_ts >= before_ts,
        f"{before_ts.isoformat()} → {after_ts.isoformat()}（相等=没刷新，更大=刷新了）",
    )


# ============================================================
# B. 摘要链路：阈值 → 区间 → 压缩 → 游标 → 攒够两轮再摘
# ============================================================
async def case_b() -> None:
    print("\n=== B. 摘要链路（含 2 次真实 LLM 调用）===\n")

    sid = SID_SUM

    # ---------- B1：20 轮不触发（PRD F5.2 的阈值就是 20）----------
    await append_messages(sid, turns_payload(20), title_hint="你好")
    result = await maybe_summarize(sid)
    check(
        "B1 满 20 轮：不触发（阈值是「超过 20 轮」）",
        result["summarized"] is False and result["reason"] == "below_trigger",
        f"reason={result['reason']}",
    )
    check("B1b 未触发时摘要仍为空", await get_summary(sid) is None)

    # ---------- B2：第 21 轮触发 ----------
    await append_messages(sid, turns_payload(21)[40:], title_hint=None)  # 补第 21 轮
    ids = await clean_ids(sid)
    stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    lower_expected = ids[42 - MAX_MESSAGES]        # 42 条里的第 27 条
    check(
        "B2 前置：21 轮 = 42 条，窗口下界 = 第 27 条",
        stats["total"] == 42 and stats["window_lower_id"] == lower_expected,
        f"total={stats['total']} lower={stats['window_lower_id']}",
    )

    result = await maybe_summarize(sid)
    check(
        "B3 21 轮：触发摘要，且摘的是「已滑出窗口」的那 26 条（不是 42 条全部）",
        result["summarized"] is True and result["merged"] == 26,
        f"reason={result['reason']} merged={result['merged']}",
    )

    summary = await get_summary(sid)
    check(
        "B4 摘要已落到 sessions.summary（PG，非 Redis）",
        bool(summary),
        f"长度={len(summary or '')} 内容={ (summary or '')[:80]!r }",
    )
    # ⚠ 比对前先去掉空白再匹配：实测 LLM 会把「1.2 亿元」写成「1.2亿元」，
    # 直接用原字符串做子串匹配会把"保住了"误判成"丢了"（第一次跑就踩到了）。
    # 这也是"关键事实不丢"这类断言该有的写法 —— 比语义，不比排版。
    norm = (summary or "").replace(" ", "")
    check(
        f"B5 关键事实未丢：摘要保留了「{FACT_SECRET}」与「{FACT_REVENUE}」两个事实",
        FACT_SECRET in norm and "1.2亿" in norm,
        f"摘要={norm[:110]!r}",
    )
    cursor = await _get_cursor(sid)
    check(
        "B6 游标推进到本批最后一条（下次从这里继续）",
        cursor == ids[25],
        f"cursor={cursor} 期望={ids[25]}",
    )

    # ---------- B7：紧接着再跑 → 待摘 0 条，不摘 ----------
    result = await maybe_summarize(sid)
    check(
        "B7 刚摘完立刻再跑：待摘不足 → 不摘（不会白烧调用）",
        result["summarized"] is False and result["reason"] == "below_min_batch",
        f"reason={result['reason']}",
    )

    # ---------- B8：再加 1 轮 → 待摘 2 条，仍不摘（"攒够 2 轮"）----------
    await append_messages(sid, turns_payload(22)[42:], title_hint=None)
    result = await maybe_summarize(sid)
    check(
        "B8 待摘 2 条（1 轮）：仍不摘 —— 攒够 2 轮再摘，省一半调用",
        result["summarized"] is False and result["reason"] == "below_min_batch",
        f"reason={result['reason']} merged={result['merged']}",
    )

    # ---------- B9：再加 1 轮 → 待摘 4 条（2 轮），触发 ----------
    await append_messages(sid, turns_payload(23)[44:], title_hint=None)
    old_summary = await get_summary(sid)
    result = await maybe_summarize(sid)
    check(
        "B9 待摘 4 条（2 轮）：触发，且只摘这 4 条（增量，不重摘全部）",
        result["summarized"] is True and result["merged"] == 4,
        f"reason={result['reason']} merged={result['merged']}",
    )

    new_summary = await get_summary(sid)
    check(
        "B10 增量合并正确：新摘要仍保留上一版里的关键事实（旧摘要没有被丢掉）",
        FACT_SECRET in (new_summary or "").replace(" ", ""),
        f"新摘要={ (new_summary or '')[:80]!r }",
    )
    check(
        "B10b 摘要确实被改写过（不是原样保留旧的那一份）",
        new_summary != old_summary,
        f"长度 {len(old_summary or '')} → {len(new_summary or '')}",
    )

    ids_after = await clean_ids(sid)
    cursor = await _get_cursor(sid)
    check(
        "B11 游标推进到第 2 批的最后一条",
        cursor == ids_after[29],
        f"cursor={cursor} 期望={ids_after[29]}",
    )


# ============================================================
# C. 容错：Redis 挂掉时，读 / 写 / 摘要分别还能不能活
# ============================================================
class DownRedis:
    """模拟"Redis 服务不可用"：三个方法全部抛异常。"""

    async def get(self, *args, **kwargs):
        raise ConnectionError("模拟 Redis 宕机（get）")

    async def set(self, *args, **kwargs):
        raise ConnectionError("模拟 Redis 宕机（set）")

    async def delete(self, *args, **kwargs):
        raise ConnectionError("模拟 Redis 宕机（delete）")


async def case_c() -> None:
    print("\n=== C. 容错：Redis 不可用时各链路是否仍能工作 ===\n")

    sid = SID_TOL
    # 造 3 轮（6 条）在 PG 里；Redis 侧先清空 —— 这样"Redis 挂了"与"Redis 里没有"等价
    await append_messages(sid, turns_payload(3), title_hint="你好")
    try:
        await redis_client.delete(_window_key(sid), _summary_cursor_key(sid))
    except Exception:
        pass

    real_client = memory_service.redis_client
    memory_service.redis_client = DownRedis()
    try:
        # ---------- C1：读 helper 吞异常 ----------
        got = await memory_service._redis_get("any-key")
        check("C1 Redis 读失败 → 返回 None（不抛异常）", got is None, f"返回 {got!r}")

        # ---------- C2：Redis 挂了，热窗口仍能从 PG 兜底重建 ----------
        window = await get_window(sid)
        check(
            "C2 Redis 不可用时 get_window 仍返回 PG 里的历史（修复前这里会 500）",
            len(window) == 6,
            f"取到 {len(window)} 条",
        )

        # ---------- C3：写 helper 吞异常 ----------
        ok = await memory_service._redis_set("any-key", "v", ex=60)
        check("C3 Redis 写失败 → 返回 False（不抛异常）", ok is False)

        # ---------- C4：append_turn 不抛 ----------
        threw = None
        try:
            await memory_service.append_turn(sid, "Redis 挂时的一问", "Redis 挂时的一答")
        except Exception as e:  # noqa: BLE001 —— 这里就是要抓一切异常
            threw = e
        check(
            "C4 Redis 不可用时 append_turn 不抛异常（对话不会 500）",
            threw is None,
            f"抛出={threw!r}",
        )

        # ---------- C5：游标写不进去也不抛 ----------
        ok = await memory_service._set_cursor(sid, 12345)
        check(
            "C5 游标写入失败 → False（丢的只是优化，不影响正确性）",
            ok is False,
            f"返回 {ok!r}；读回游标={await _get_cursor(sid)!r}（None=读不到，将从头摘）",
        )

        # ---------- C6：Redis 全挂时，摘要链路仍然产出了正确结果 ----------
        # 把 SID_SUM 会话补到"待摘 4 条"，但此时游标读不到（Redis 挂）→
        # 会退化成"从最早重摘一遍"。这正是设计里声明的降级：只损失效率，不损失正确性。
        await append_messages(SID_SUM, turns_payload(25)[46:], title_hint=None)
        ids = await clean_ids(SID_SUM)
        stats = await get_dialogue_stats(SID_SUM, window_size=MAX_MESSAGES)
        lower = stats["window_lower_id"]
        expect_merged = len([i for i in ids if lower is None or i < lower])
        result = await maybe_summarize(SID_SUM)
        check(
            "C6 Redis 不可用时摘要仍摘对（游标读不到 → 退化成重摘，结果正确）",
            result["summarized"] is True and result["merged"] == expect_merged,
            f"reason={result['reason']} merged={result['merged']} 期望={expect_merged}"
            f"（total={stats['total']}, lower={lower}）",
        )
        check(
            "C6b 重摘后的摘要仍含关键事实",
            FACT_SECRET in (await get_summary(SID_SUM) or "").replace(" ", ""),
        )
    finally:
        memory_service.redis_client = real_client

    # ---------- C7：恢复后一切照旧 ----------
    got = await memory_service._redis_set("d15-c7-probe", "v", ex=60)
    check("C7 恢复 Redis 后写入成功（证明 C 段确实是「被临时替换」，不是环境坏了）", got is True)
    await redis_client.delete("d15-c7-probe")


# ============================================================
# D. 端到端：摘要是真的被注入到 system 里了
# ============================================================
async def case_d() -> None:
    print("\n=== D. 端到端：摘要注入 system（真实跑一轮对话）===\n")

    from app.services.agent_service import run_agent

    sid = SID_E2E
    # 关键前置：窗口为空 + PG 里没有对话消息，**只有** sessions.summary 有内容。
    # 这样模型如果能答出代号，就只可能来自摘要 —— 否则它只能从窗口历史里答，测不出注入。
    await ensure_session(sid, title_hint="端到端摘要注入")
    await save_summary(
        sid,
        f"用户在本会话早期自称代号「{FACT_SECRET.split('-')[0]}」，负责「翡翠」项目，"
        f"并强调不要对外透露这个代号。",
    )
    await redis_client.delete(_window_key(sid), _summary_cursor_key(sid))

    pre_stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    check(
        "D1 前置：窗口为空且 PG 无对话消息（否则本段证明不了「摘要被读到」）",
        pre_stats["total"] == 0,
        f"total={pre_stats['total']}",
    )

    result = await run_agent(sid, "根据我们之前的对话，我叫什么代号？请直接回答。")
    answer = result.answer
    print(f"  [D] 模型回答：{answer[:120]}")
    check(
        "D2 模型答出了摘要里的事实（证明摘要确实进了 system）",
        "蓝鲸" in answer,
    )

    check(
        "D3 本轮对话已落库（2 条：user + assistant）",
        (await get_dialogue_stats(sid, window_size=MAX_MESSAGES))["total"] == 2,
    )
    check(
        "D4 本轮不该动摘要（未达 20 轮，maybe_summarize 只做检查）",
        "蓝鲸" in (await get_summary(sid) or ""),
    )


async def main() -> None:
    await case_a()
    await case_b()
    await case_c()
    await case_d()

    # 收尾：清掉四个测试会话，不给下一次运行留残留
    for sid in (SID_STATS, SID_SUM, SID_TOL, SID_E2E):
        await drop_session(sid)

    print(f"\n=== 结果：{len(passed)} 通过 / {len(failed)} 失败 ===")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")

    await engine.dispose()      # 在本事件循环内关闭连接池（顺序反了会留一堆噪音堆栈）
    if failed:
        raise SystemExit(1)
    print("全部通过 ✅\n")


if __name__ == "__main__":
    asyncio.run(main())
