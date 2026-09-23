"""
D14 验证：会话与消息落库（session_service 层）
===============================================
跑法（项目根目录）：uv run python -m scripts.d14_sessions_verify

为什么单独写脚本而不是手点 API：
这一步要验证的是「落库行为」——有没有真的写进去、外键级联有没有生效。
这类问题在 HTTP 层看起来一切正常（照样返回 200），只有回库里查行数才暴露。
D12 那次「删文档留下孤儿切片」就是这么漏掉的：接口返回 204，表里却还有数据。

断言设计原则（铁律 9）：每条断言都要能回答"什么情况下它会假通过"。
本脚本的写法是「先确认非空、再确认变化」，避免空数据恒满足。
"""

import asyncio
import uuid

from sqlalchemy import delete, func, select

from app.core.db import async_session_factory
from app.models.message import Message
from app.models.session import Session
from app.services.session_service import (
    append_messages,
    ensure_session,
    get_messages,
    list_sessions,
)

# 固定测试会话 id：每次跑都用同一个，避免反复堆积垃圾数据
TEST_SID = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d14.verify")
# 对照会话：用来验证"最近活跃的排在前面"这条排序行为，没有对照就没法断言
CONTROL_SID = uuid.uuid5(uuid.NAMESPACE_DNS, "agentforge.d14.verify.control")
TEST_TITLE_HINT = "帮我查一下上个季度的营收情况"

# 一轮完整对话的三条消息：user → assistant(带 tool_calls) → tool
TURN_MESSAGES = [
    {"role": "user", "content": "上周的营收是多少？"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "search_knowledge", "arguments": "{}"}}
    ]},
    {"role": "tool", "content": "上周营收 1280 万元", "tool_calls": None},
]

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果（失败不中断，最后统一汇总）。"""
    (passed if ok else failed).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{('  —— ' + detail) if detail else ''}")


async def count_messages(sid: uuid.UUID) -> int:
    """查该会话在 messages 表里的实际行数（直接查库，不信函数的返回值）。"""
    async with async_session_factory() as db:
        return (
            await db.execute(
                select(func.count()).select_from(Message).where(Message.session_id == sid)
            )
        ).scalar_one()


async def drop_session(sid: uuid.UUID) -> None:
    """删除会话（连带删消息，靠 DB 的 ON DELETE CASCADE）。"""
    async with async_session_factory() as db:
        await db.execute(delete(Session).where(Session.id == sid))
        await db.commit()


async def run_service_layer() -> None:
    """A 部分：数据层 —— session_service 的五个出口。"""
    print("\n=== D14 验证 A：会话与消息落库（数据层）===\n")

    # 前置清理：保证从"干净状态"开始，否则上一轮残留会让断言假通过
    await drop_session(TEST_SID)
    await drop_session(CONTROL_SID)
    before = await count_messages(TEST_SID)
    print(f"[准备] 测试会话 {TEST_SID} 残留消息 = {before} 条")
    check("前置清理生效（起始 0 条）", before == 0, f"实际 {before}")

    # ---- V1：会话不存在则创建，存在则不动 ----
    print("\n[V1] ensure_session 幂等")
    created_first = await ensure_session(TEST_SID, title_hint=TEST_TITLE_HINT)
    created_again = await ensure_session(TEST_SID, title_hint="换一个标题试试")
    check("首次调用返回「新建」", created_first is True, f"返回 {created_first}")
    check("二次调用返回「已存在」", created_again is False, f"返回 {created_again}")

    sessions = await list_sessions(limit=200)
    mine = [s for s in sessions if s["id"] == str(TEST_SID)]
    check("会话出现在列表里", len(mine) == 1, f"命中 {len(mine)} 条")
    check(
        "标题取自首条消息且未被二次覆盖",
        bool(mine) and mine[0]["title"] == TEST_TITLE_HINT,
        f"实际 {mine[0]['title'] if mine else '（无）'}",
    )

    # ---- V2：消息全量落库（关键：tool 消息也要进去）----
    print("\n[V2] append_messages 全量落库")
    # 先造一个"更早活跃"的对照会话，V3 要靠它验证「最近活跃在前」的排序
    await ensure_session(CONTROL_SID, title_hint="对照组：更早活跃的会话")
    await asyncio.sleep(0.05)      # 拉开时间差，避免两个事务的时间戳撞在同一微秒上
    written = await append_messages(TEST_SID, TURN_MESSAGES)
    after = await count_messages(TEST_SID)
    check("返回值等于入参条数", written == len(TURN_MESSAGES), f"返回 {written}")
    check("库里实际行数非零且相等", after == len(TURN_MESSAGES) and after > 0, f"实际 {after} 行")

    stored = await get_messages(TEST_SID)
    roles = [m["role"] for m in stored]
    check(
        "顺序为 user → assistant → tool",
        roles == ["user", "assistant", "tool"],
        f"实际 {roles}",
    )
    check(
        "tool 消息也进了库（全量留痕）",
        any(m["role"] == "tool" for m in stored),
        f"roles={roles}",
    )
    assistant_row = next((m for m in stored if m["role"] == "assistant"), None)
    check(
        "assistant 的 tool_calls 被保存",
        bool(assistant_row) and bool(assistant_row["tool_calls"]),
        f"实际 {assistant_row['tool_calls'] if assistant_row else '（无该行）'}",
    )

    # ---- V3：updated_at 真的被刷新（用对照会话做相对位置断言）----
    print("\n[V3] 刷新会话时间戳")
    sessions = await list_sessions(limit=200)
    ids = [s["id"] for s in sessions]
    both_present = str(TEST_SID) in ids and str(CONTROL_SID) in ids
    check("测试会话与对照会话都在列表里", both_present, f"列表共 {len(ids)} 个会话")
    if both_present:
        test_pos, control_pos = ids.index(str(TEST_SID)), ids.index(str(CONTROL_SID))
        check(
            "刚写入消息的会话排在对照会话之前",
            test_pos < control_pos,
            f"测试会话第 {test_pos + 1} 位 / 对照会话第 {control_pos + 1} 位",
        )
    else:
        check("刚写入消息的会话排在对照会话之前", False, "缺失会话，无法比较（避免空集假通过）")

    # ---- V4：级联删除（D12 的教训：断言要打在行为上）----
    print("\n[V4] 删会话 → 消息一并清掉")
    before_delete = await count_messages(TEST_SID)
    await drop_session(TEST_SID)
    after_delete = await count_messages(TEST_SID)
    check(
        "删除前消息数非零（防止空集假通过）",
        before_delete > 0,
        f"删除前 {before_delete} 行",
    )
    check("删除后消息清零（级联生效）", after_delete == 0, f"删除后 {after_delete} 行")

    # 收尾：清掉对照会话，不给下一次运行留残留
    await drop_session(CONTROL_SID)

    # 关键（d11_http_verify 踩过的坑）：接下来 B 部分用 TestClient，它会在
    # **另一个线程**里另起事件循环。连接池里残留的 asyncpg 连接绑定在**当前这个
    # loop** 上，直接复用会报 "attached to a different loop"。
    # 所以必须先关掉连接池 —— 但只能在**本 loop 内**关：
    # 换个 loop 再 dispose，就是在别人的 loop 里关自己的连接，
    # 会抛 "RuntimeError: Event loop is closed"（第一版就是这么写的）。
    from app.core.db import engine

    await engine.dispose()


# ============================================================
# B. HTTP 层：真跑一轮对话，验证「落库 → 查询接口」这条链路
# ============================================================
HTTP_USER_MSG = "请记住：我们公司今年的营收目标是 1.2 亿元。"


def run_http_layer() -> None:
    """
    走真实 HTTP 边界跑一轮对话，再用两个查询接口把它读回来。

    ⚠ 这一部分会**真的调用 LLM**（约 10-30 秒）。为什么非要真跑：
    只测 session_service 只能证明「函数本身能用」，证明不了
    「/api/chat 这条链路上真的调了它」—— 这正是 D12 那次教训的形态
    （接口返回正常，数据却没落下去）。断言必须打在端到端行为上。

    ⚠ 本部分产生的会话**不清理**（HTTP 层没有删除接口）。
    要清可用：
        docker exec agentforge-db psql -U agentforge -d agentforge \\
          -c "DELETE FROM sessions WHERE title LIKE '请记住%';"
    """
    from fastapi.testclient import TestClient

    from app.main import app

    print("\n=== D14 验证 B：HTTP 层（真实跑一轮对话）===\n")

    with TestClient(app) as client:
        # ---- B1：发一轮对话，拿到 session_id ----
        r = client.post("/api/chat", json={"message": HTTP_USER_MSG})
        data = r.json() if r.status_code == 200 else {}
        sid = data.get("session_id", "")
        print(f"[B1] POST /api/chat → HTTP {r.status_code}  session_id={sid}")
        check("对话返回 200", r.status_code == 200, f"实际 {r.status_code}")
        check("session_id 是 36 位标准 UUID", len(sid) == 36, f"实际长度 {len(sid)}")
        check("回答非空", bool(data.get("answer")), f"answer={data.get('answer', '')[:40]!r}")

        if not sid:
            print("  ⚠ 没拿到 session_id，后续用例无法继续")
            return

        # ---- B2：会话列表里能查到它 ----
        r2 = client.get("/api/sessions", params={"limit": 200})
        items = r2.json() if r2.status_code == 200 else []
        mine = [s for s in items if s["id"] == sid]
        print(f"[B2] GET /api/sessions → HTTP {r2.status_code}  列表共 {len(items)} 个会话")
        check("会话出现在列表中", len(mine) == 1, f"命中 {len(mine)} 条")
        check(
            "标题取自首条用户消息（前 30 字）",
            bool(mine) and mine[0]["title"] == HTTP_USER_MSG[:30],
            f"实际 {(mine[0]['title'] if mine else '（无）')!r}",
        )

        # ---- B3：历史消息读回来 ----
        r3 = client.get(f"/api/sessions/{sid}/messages")
        msgs = r3.json() if r3.status_code == 200 else []
        roles = [m["role"] for m in msgs]
        print(f"[B3] GET /api/sessions/{{id}}/messages → HTTP {r3.status_code}  "
              f"{len(msgs)} 条  roles={roles}")
        check("历史非空且首条是 user", bool(msgs) and roles[0] == "user", f"roles={roles}")
        check(
            "末条是 assistant（回答也落库了）",
            bool(msgs) and roles[-1] == "assistant",
            f"roles={roles}",
        )
        check(
            "user 消息内容与提问逐字一致",
            bool(msgs) and msgs[0]["content"] == HTTP_USER_MSG,
            f"实际 {(msgs[0]['content'][:24] if msgs else '（无）')!r}",
        )

        # ---- B4：不存在的会话 → 404（而不是空数组）----
        ghost = uuid.uuid4()
        r4 = client.get(f"/api/sessions/{ghost}/messages")
        print(f"[B4] 不存在的会话 → HTTP {r4.status_code}（期望 404）")
        check("会话不存在返回 404", r4.status_code == 404, f"实际 {r4.status_code}")

        # ---- B5：无横杠写法归一化到同一会话 ----
        r5 = client.get(f"/api/sessions/{sid.replace('-', '')}/messages")
        msgs5 = r5.json() if r5.status_code == 200 else []
        print(f"[B5] 无横杠写法 → HTTP {r5.status_code}  {len(msgs5)} 条（期望与 B3 相同）")
        check(
            "无横杠写法查到同一批消息",
            r5.status_code == 200 and len(msgs5) == len(msgs),
            f"{len(msgs5)} 条 vs B3 的 {len(msgs)} 条",
        )


def main() -> None:
    # A 部分自己管一个事件循环，并且**在这个循环结束前**关掉连接池
    #（dispose 写在 run_service_layer 末尾，不在这里 —— 原因见那里的注释）。
    asyncio.run(run_service_layer())

    # B 部分用 TestClient（另一个线程 + 新的 loop），此时连接池是干净的。
    run_http_layer()

    print(f"\n=== 结果：{len(passed)} 通过 / {len(failed)} 失败 ===")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        raise SystemExit(1)
    print("全部通过 ✅\n")


if __name__ == "__main__":
    # 注意：main 是同步函数（它内部自己管事件循环 —— 数据层一段、HTTP 层一段，
    # 两段之间要 dispose 连接池），所以这里不能写 asyncio.run(main())。
    main()
