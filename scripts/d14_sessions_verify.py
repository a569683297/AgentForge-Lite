"""
D14 验证：会话与消息落库（session_service 层 + HTTP 链路 + 落库形态）
====================================================================
跑法（项目根目录）：uv run python -m scripts.d14_sessions_verify

三段各管一件事：
    A 数据层  —— session_service 的五个出口本身对不对（不碰 LLM，秒级）
    B HTTP 层 —— /api/chat 这条链路上真的调了落库吗（真跑两轮对话，会调 LLM）
    C 回库核对 —— 表里到底存成了什么样（本轮修的两个缺陷只有回库才看得见）

为什么单独写脚本而不是手点 API：
这一步要验证的是「落库行为」——有没有真的写进去、外键级联有没有生效。
这类问题在 HTTP 层看起来一切正常（照样返回 200），只有回库里查行数才暴露。
D12 那次「删文档留下孤儿切片」就是这么漏掉的：接口返回 204，表里却还有数据。

C 段守的是两个已修缺陷，属于回归断言（改回去就会红）：
    ① plan 抢答：一轮里"没有 tool_calls 的 assistant"必须恰好 1 条（修复前恒为 2 条）
    ② tool_calls 语义：非工具消息的 tool_calls 必须是 SQL NULL（修复前是 JSON null）

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

# 本脚本播种的知识库文档名（2026-09-28 新增）。
# 它既是"这批数据归我管"的标记，也是**清理时的作用域边界** ——
# 原先这里调 delete_all_documents() 清空全库，会把 D21 的长期语料一起删掉。
VERIFY_PREFIX = "d14-verify"

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
# 第二轮用"必须查文档"的问题：专门覆盖「有工具路径」——
# 修复前这条路一轮落 3 条 assistant（plan 调工具 + plan 抢答 + answer）
RAG_USER_MSG = "「琥珀」这个项目是哪个组负责的？"
# 该问题的答案只存在于下面这篇语料里（刻意用模型不可能知道的虚构事实：
# 回答正确 = 确实查了文档，而不是模型编的。沿用 D10/D11 的语料）
RAG_DOCS = [
    "公司内部项目管理规定：代号为「琥珀」的项目由星河算法组负责，"
    "代号为「翡翠」的项目由山海基础组负责，两个项目组均直接向 CTO 汇报。",
    "公司团建经费标准：每人每季度上限为 800 元，"
    "由部门助理统一申请，需附活动签到表与消费明细。",
]


async def prepare_corpus() -> None:
    """把 B 段要用的知识库语料准备好。

    为什么要在脚本里做：B 段的「有工具路径」依赖检索能真的命中，
    而知识库内容会随别的验证脚本（d10/d11/d12）跑动而变 ——
    靠"跑之前库里恰好有什么"就是隐性依赖，会变成随机假失败。

    ⚠ 2026-09-28 改：清理范围自限
        原先调 `delete_all_documents()` 清空全库 —— 这个脚本**只想清掉自己
        那篇 `d14-verify`**，却会把 D21 的 8 篇长期语料一起删光。
        现在改为按 `VERIFY_PREFIX` 前缀删除。
        （顺带：d10/d11/d12 也已改成按前缀删除，所以上面说的
          "会被别的脚本跑动而变"这个隐性依赖，实际上被这轮改动削弱了 ——
          但**保留自己播种**，脚本仍然自包含，不依赖执行顺序。）
    """
    from app.core.db import engine
    from app.services.document_service import delete_documents_by_prefix, ingest_texts

    try:
        await delete_documents_by_prefix(VERIFY_PREFIX)
        _, n = await ingest_texts(RAG_DOCS, filename=VERIFY_PREFIX)
        print(f"[准备] 知识库语料已更新 → {n} 个切片")
    finally:
        # 与 A 段同理：本 loop 用完就把连接池关掉，
        # 否则 B 段 TestClient 的另一个 loop 会拿到绑在死 loop 上的连接
        await engine.dispose()


def run_http_layer() -> list[dict]:
    """
    走真实 HTTP 边界跑两轮对话，再用查询接口把它读回来。

    ⚠ 这一部分会**真的调用 LLM**（两轮，约 20-60 秒）。为什么非要真跑：
    只测 session_service 只能证明「函数本身能用」，证明不了
    「/api/chat 这条链路上真的调了它」—— 这正是 D12 那次教训的形态
    （接口返回正常，数据却没落下去）。断言必须打在端到端行为上。

    ⚠ 本部分产生的会话**不清理**（HTTP 层没有删除接口）。
    要清可用：
        docker exec agentforge-db psql -U agentforge -d agentforge \\
          -c "TRUNCATE TABLE messages, sessions RESTART IDENTITY CASCADE;"

    Returns:
        本轮跑出的会话清单（交给 C 部分回库核对落库形态）
    """
    from fastapi.testclient import TestClient

    from app.main import app

    print("\n=== D14 验证 B：HTTP 层（真实跑两轮对话）===\n")

    records: list[dict] = []

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

        records.append({"label": "轮1·无需工具", "session_id": sid})

        # ---- B6：再跑一轮"必须检索"的对话，供 C 部分验证有工具路径 ----
        r6 = client.post("/api/chat", json={"message": RAG_USER_MSG})
        d6 = r6.json() if r6.status_code == 200 else {}
        sid6 = d6.get("session_id", "")
        n_src = len(d6.get("sources", []))
        print(f"[B6] RAG 轮 → HTTP {r6.status_code}  session_id={sid6}  来源数={n_src}")
        check("RAG 轮返回 200", r6.status_code == 200, f"实际 {r6.status_code}")
        check("RAG 轮拿到了来源（检索链路通）", n_src > 0, f"sources={n_src}")
        if sid6:
            records.append({"label": "轮2·有工具", "session_id": sid6})

        # 关键：在 TestClient **自己的 loop 内**把连接池关掉。
        # TestClient 另起线程、另起 loop，池里的连接都绑在那个 loop 上；
        # 等它退出、loop 关闭之后再由 C 段去 dispose，就等于在**别人的 loop 里关连接**，
        # 日志会冒出一段 "Exception closing connection / Event loop is closed" 的红字
        # （实测过：不影响断言结果，但会掩盖真问题）。portal 是它的阻塞式 portal，
        # 用它把 dispose 送回那个 loop 执行。
        from app.core.db import engine

        client.portal.call(engine.dispose)

    return records


# ============================================================
# C. 回库核对：HTTP 层跑完之后，直接查表验证「落库形态」
# ============================================================
async def run_db_layer(records: list[dict]) -> None:
    """
    B 部分的断言只看到「接口返回了什么」，看不到「表里到底存成了什么样」。
    本轮修的两个缺陷，恰好都只有回库才看得见：

      ① **plan 抢答**（刚修）→ 一轮里"没有 tool_calls 的 assistant"必须**恰好 1 条**。
         修复前是 2 条（plan 抢答 + answer 各一条），不管这一轮有没有用工具。
      ② **tool_calls 存成 JSON null**（刚修）→ 非工具消息的 tool_calls 必须是
         **SQL NULL**，`IS NULL` 判定要为真。修复前这里恒为假。
    """
    from sqlalchemy import text as sql_text

    from app.core.db import engine

    # 正常情况：B 段结束前已经在**它自己的 loop 内**把连接池关干净了（见那里注释）。
    # 这里再 dispose 一次是兜底 —— 池为空时它是无副作用的空操作。
    await engine.dispose()

    print("\n=== D14 验证 C：回库核对落库形态 ===\n")

    for rec in records:
        sid = uuid.UUID(rec["session_id"])
        async with async_session_factory() as db:
            rows = (
                await db.execute(
                    sql_text(
                        "SELECT role, (tool_calls IS NULL) AS tc_is_null, "
                        "coalesce(length(content), 0) AS clen "
                        "FROM messages WHERE session_id = :sid ORDER BY id"
                    ),
                    {"sid": sid},
                )
            ).all()

        roles = [r.role for r in rows]
        finals = [r for r in rows if r.role == "assistant" and r.tc_is_null]
        tool_rows = [r for r in rows if r.role == "tool"]
        path = "有工具路径" if tool_rows else "无工具路径"
        print(f"\n[C] {rec['label']}（{path}）roles={roles}")

        check(
            f"{rec['label']}：最终回答恰好 1 条",
            len(finals) == 1,
            f"实际 {len(finals)} 条无 tool_calls 的 assistant —— 修复前这里恒为 2 条",
        )
        check(
            f"{rec['label']}：非 assistant 消息的 tool_calls 是 SQL NULL",
            all(r.tc_is_null for r in rows if r.role != "assistant"),
            f"涉及 {sum(1 for r in rows if r.role != 'assistant')} 行 user/tool 消息",
        )
        check(
            f"{rec['label']}：最终回答内容非空",
            bool(finals) and finals[0].clen > 0,
            f"长度 {finals[0].clen if finals else 0}",
        )
        # 这一条是"补充证据"，不作断言：LLM 偶尔可能判断该轮不需要检索，
        # 那属于模型决策差异、不是代码缺陷，硬断言会造成假失败。
        print(f"     └ 该轮工具调用 {len(tool_rows)} 次；"
              f"assistant 总计 {sum(1 for r in rows if r.role == 'assistant')} 条"
              f"（修复前无工具轮 2 条 / 有工具轮 3 条）")

    # 本 loop 的活干完了，立刻把连接池关掉（本脚本的固定纪律，见 main 的注释）。
    # ⚠ 这一行是 2026-09-28 加的：cleanup_corpus() 会用 asyncio.run 另起一个 loop，
    #   如果这里不 dispose，那个新 loop 会拿到绑在本 loop 上的连接 →
    #   `got Future ... attached to a different loop`（实测过，脚本直接 exit=1）。
    #   同族坑：d11_http_verify 的 TestClient 段、d19 的 C 段。
    from app.core.db import engine

    await engine.dispose()


async def cleanup_corpus() -> None:
    """跑完把自己的语料收干净（2026-09-28 新增）。

    为什么必须收：「别删别人的」只解决了一半问题，另一半是**别留下自己的**。
    残留的 `d14-verify` 会一直躺在知识库里被检索到，而 D22/D23 的评测题
    问的是 D21 那 8 篇语料的内容 —— 被这些测试文档命中，成绩就没法解释了。

    断言走 check() 记入统一汇总，所以清理不干净会让整个脚本非 0 退出。
    """
    from app.core.db import engine
    from app.services.document_service import (
        count_documents_by_prefix,
        delete_documents_by_prefix,
    )

    await delete_documents_by_prefix(VERIFY_PREFIX)
    left = await count_documents_by_prefix(VERIFY_PREFIX)
    check("Z1 本脚本语料已收干净（0 残留）", left == 0, f"实际残留={left}")
    await engine.dispose()


def main() -> None:
    # A 部分自己管一个事件循环，并且**在这个循环结束前**关掉连接池
    #（dispose 写在 run_service_layer 末尾，不在这里 —— 原因见那里的注释）。
    asyncio.run(run_service_layer())

    # B 段要用的知识库语料：脚本自包含准备，不依赖"跑之前库里恰好有什么"
    asyncio.run(prepare_corpus())

    # B 部分用 TestClient（另一个线程 + 新的 loop），此时连接池是干净的。
    records = run_http_layer()

    # C 部分再开一个 loop 回库核对（内部会先 dispose 掉 B 留下的死连接）。
    asyncio.run(run_db_layer(records))

    # 跑完把自己的语料收干净（2026-09-28 新增）——
    # 放在 C 段之后：C 段还要检索，不能提前清掉。
    asyncio.run(cleanup_corpus())

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
