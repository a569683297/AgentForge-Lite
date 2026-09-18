"""
D11 验证：引用定位（回答里的 [n] 能映射回原文来源）
====================================================
分两层，因为沙箱会拦 .env，纯逻辑层可以独立跑：

A. 纯逻辑层（不依赖数据库/LLM/配置）
   A1. 编号全局递增 —— 一次请求内多次检索不重复从 1 开始
   A2. 越界检测   —— 能抓出 LLM 引用了不存在的编号，且不误伤 markdown 链接
   A3. 并发隔离   —— 两个并发请求各持一份收集器（contextvar 的关键性质）

B. 端到端层（需要 .env + Docker 的 db/redis）
   B1. 问只有文档里才有答案的问题 → 回答带 [1]，且 [1] 能查出 source/content
   B2. 一次请求内连续两次检索 → 编号接续而非重来
   B3. 并发两个会话 → 各自的来源编号都从 1 连续，未串数据

用法：
    uv run python -m scripts.d11_citation_verify
"""

import asyncio
import uuid

from app.core.citation import check_citations, get_sources, record_sources, reset_sources


def new_session(prefix: str) -> str:
    """
    每次运行都用一个全新会话 ID。

    ⚠ 这里踩过一次坑：会话 ID 写死（如 "d11-e2e-1"）会让第二次运行读到
    Redis 里上一轮的问答历史，LLM 直接从历史里回答、**不再调用工具**，
    于是 sources 为空 —— 看起来像"收集器坏了"，其实是记忆层的影响。
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


# ============================================================
# A. 纯逻辑层
# ============================================================
def case_a1_offset_increment() -> bool:
    """编号全局递增：第二批不能从 1 重新开始"""
    section("A1. 多批检索的编号递增")
    reset_sources()

    o1 = record_sources(
        [
            {"content": "员工出差需在 5 个工作日内提交报销单", "source": "finance.md", "similarity": 0.78},
            {"content": "公司年假政策：入职满一年享 5 天", "source": "hr.md", "similarity": 0.41},
        ]
    )
    o2 = record_sources(
        [{"content": "团建经费每人每季度 800 元", "source": "finance.md", "similarity": 0.65}]
    )

    srcs = get_sources()
    indexes = [s["index"] for s in srcs]
    print(f"  第一批 offset = {o1}（应从 0 开始）")
    print(f"  第二批 offset = {o2}（应是 2，即接在第一批后面）")
    print(f"  累计来源编号 = {indexes}")
    for s in srcs:
        print(f"    [{s['index']}] {s['source']}  相关度 {s['similarity']}")

    ok = o1 == 0 and o2 == 2 and indexes == [1, 2, 3]
    print(f"  [判定] {'✅ 编号全局递增，两批不会撞车' if ok else '❌ 编号错乱'}")
    return ok


def case_a2_check_citations() -> bool:
    """越界检测：抓幻觉引用，同时不误伤 markdown 链接"""
    section("A2. 越界引用检测")
    cases = [
        ("根据公司制度，报销需在 5 个工作日内提交 [1]，并附发票 [2]。", 3, []),
        ("根据 [1] 和 [5]，出差需审批。", 3, [5]),
        ("根据 [0]，这是非法编号。", 3, [0]),
        ("资料里没有相关内容。", 3, []),
        ("详见 [员工手册](http://x.com/handbook) 第 3 节 [1]", 3, []),
    ]
    all_ok = True
    for answer, total, expect in cases:
        got = check_citations(answer, total)
        ok = got == expect
        all_ok = all_ok and ok
        mark = "✅" if ok else "❌"
        print(f"  {mark} 来源数={total} 期望越界={expect} 实际={got}")
        print(f"      回答：{answer}")
    return all_ok


async def case_a3_concurrent_isolation() -> bool:
    """并发隔离：每个请求在自己的 task 内重新绑定收集器"""
    section("A3. 并发请求的收集器隔离（纯逻辑）")

    async def fake_request(tag: str) -> list[dict]:
        reset_sources()  # ← 关键：绑定动作发生在各自 task 内部
        record_sources(
            [{"content": f"{tag}-内容", "source": f"{tag}.md", "similarity": 0.5}]
        )
        await asyncio.sleep(0.02)  # 模拟 IO，让两个请求真正交错
        return get_sources()

    r1, r2 = await asyncio.gather(fake_request("A"), fake_request("B"))
    print(f"  请求A 看到 = {[s['index'] for s in r1]}  {[s['source'] for s in r1]}")
    print(f"  请求B 看到 = {[s['index'] for s in r2]}  {[s['source'] for s in r2]}")
    ok = (
        len(r1) == 1
        and r1[0]["source"] == "A.md"
        and len(r2) == 1
        and r2[0]["source"] == "B.md"
    )
    print(f"  [判定] {'✅ 互不污染' if ok else '❌ 串数据了'}")
    return ok


# ============================================================
# B. 端到端层
# ============================================================
DOCS = [
    (
        "d11-alpha.md",
        "公司内部项目管理规定：代号为「琥珀」的项目由星河算法组负责，项目周期两年，"
        "负责人为林工。该项目组直接向 CTO 汇报。",
    ),
    (
        "d11-beta.md",
        "公司内部项目管理规定：代号为「翡翠」的项目由山海基础组负责，项目周期一年，"
        "负责人为周工。该项目组直接向 CTO 汇报。",
    ),
]


async def prepare_kb() -> None:
    section("准备知识库")
    from app.services.document_service import delete_all_documents, ingest_texts

    await delete_all_documents()
    for filename, text in DOCS:
        _, n = await ingest_texts([text], filename=filename)
        print(f"  入库 {filename} → {n} 个切片")


async def case_b1_answer_with_citation() -> bool:
    """端到端：回答带引用编号，且编号能映射回原文"""
    section("B1. 端到端问答：回答带 [1]，且可定位原文")
    from app.services.agent_service import run_agent

    result = await run_agent(new_session("d11-e2e"), "「琥珀」这个项目是哪个组负责的？")

    print(f"  回答：{result.answer}")
    print(f"  越界引用：{result.invalid_citations or '无'}")
    print("  ---- 来源映射表（前端拿到的就是这份）----")
    for s in result.sources:
        preview = s.content[:40].replace("\n", " ")
        print(f"    [{s.index}] {s.source}  相关度 {s.similarity}  「{preview}…」")

    has_citation = "[1]" in result.answer or "[2]" in result.answer
    has_sources = len(result.sources) > 0
    no_invalid = not result.invalid_citations
    content_ok = "星河" in result.answer

    print(f"  [引用判定] {'✅ 回答含编号' if has_citation else '❌ 回答里没有引用编号'}")
    print(f"  [映射判定] {'✅ 来源表非空，[1] 能查出原文' if has_sources else '❌ 来源表为空，[1] 点不开'}")
    print(f"  [越界判定] {'✅ 无越界引用' if no_invalid else f'❌ 越界 {result.invalid_citations}'}")
    print(f"  [内容判定] {'✅ 回答来自文档（含「星河」）' if content_ok else '❌ 未命中文档内容'}")
    return has_citation and has_sources and no_invalid and content_ok


async def case_b2_multi_retrieval_in_one_request() -> bool:
    """一次请求内连续两次检索，编号接续而非重来"""
    section("B2. 一次请求内多次检索：编号全局递增")
    from app.core.citation import get_sources as gs
    from app.core.citation import reset_sources as rs
    from app.tools import execute_tool

    rs()  # 模拟 run_agent 入口
    text1 = await execute_tool("search_documents", {"query": "琥珀项目负责组"})
    text2 = await execute_tool("search_documents", {"query": "翡翠项目负责组"})

    srcs = gs()
    indexes = [s["index"] for s in srcs]

    print("  第一批工具返回（节选）：")
    print("    " + text1.split("\n")[0])
    print("  第二批工具返回（节选）：")
    print("    " + text2.split("\n")[0])
    print(f"  累计编号 = {indexes}")

    no_dup = len(indexes) == len(set(indexes))
    continuous = indexes == list(range(1, len(indexes) + 1))
    second_batch_not_restart = indexes[-1] > len(srcs) // 2 if srcs else False
    ok = no_dup and continuous and second_batch_not_restart
    print(f"  [判定] {'✅ 编号接续，两批不撞车' if ok else '❌ 编号有重复或断档'}")
    return ok


async def case_b3_concurrent_sessions() -> bool:
    """并发两个会话：来源不能串（D11 最大的工程风险）"""
    section("B3. 并发两个会话：来源不串")
    from app.services.agent_service import run_agent

    a, b = await asyncio.gather(
        run_agent(new_session("d11-conc-a"), "「琥珀」这个项目是哪个组负责的？"),
        run_agent(new_session("d11-conc-b"), "「翡翠」这个项目是哪个组负责的？"),
    )

    a_idx = [s.index for s in a.sources]
    b_idx = [s.index for s in b.sources]
    print(f"  会话A 编号={a_idx} 来源={[s.source for s in a.sources]}")
    print(f"  会话B 编号={b_idx} 来源={[s.source for s in b.sources]}")

    # ⚠ 注意断言的坑：空列表也满足「从 1 连续」，那样会假通过。
    #   所以必须先把「非空」写进条件。
    a_ok = len(a.sources) > 0 and a_idx == list(range(1, len(a_idx) + 1))
    b_ok = len(b.sources) > 0 and b_idx == list(range(1, len(b_idx) + 1))
    # 串数据的特征：两个请求共享同一个 list，条数会翻倍（单次检索上限 3 条）
    no_bleed = len(a.sources) <= 6 and len(b.sources) <= 6
    a_has = "星河算法组" in a.answer
    b_has = "山海基础组" in b.answer

    print(f"  [非空判定] A={len(a.sources)} 条  B={len(b.sources)} 条")
    print(f"  [隔离判定] {'✅ 各自从 1 连续编号，条数未翻倍' if a_ok and b_ok and no_bleed else '❌ 编号交错或条数翻倍 → 收集器串数据了'}")
    print(f"  [内容判定] A={'✅' if a_has else '❌'} B={'✅' if b_has else '❌'}")
    return a_ok and b_ok and no_bleed and a_has and b_has


async def case_b4_repeat_question() -> bool:
    """
    边界观察（不要求通过）：同一会话重复提问。

    第二轮时 Redis 记忆里已经有上一轮的问答（含 [1] 引用），
    LLM 可能直接沿用历史作答、**不再调用工具** → sources 为空，
    而回答里的 [1] 是从历史里带过来的 → 越界检测会报出来。

    这是真实存在的边界：前端拿到"有 [1] 但没有来源表"的回答时点不开。
    D11 先把它检测出来（invalid_citations 就是干这个的），
    处理策略留到 D17 评测体系量化后再定。
    """
    section("B4. 边界观察：同一会话重复提问")
    from app.services.agent_service import run_agent

    sid = new_session("d11-repeat")
    q = "「琥珀」这个项目是哪个组负责的？"

    first = await run_agent(sid, q)
    second = await run_agent(sid, q)

    print(f"  第 1 轮：来源 {len(first.sources)} 条  越界 {first.invalid_citations or '无'}")
    print(f"          {first.answer[:60]}…")
    print(f"  第 2 轮：来源 {len(second.sources)} 条  越界 {second.invalid_citations or '无'}")
    print(f"          {second.answer[:60]}…")

    if len(second.sources) == 0 and "[1]" in second.answer:
        print("  [观察] 第 2 轮未调用工具（沿用历史作答），回答里的 [1] 无来源可映射")
        print("         → invalid_citations 已正确报出，前端可据此降级显示")
    else:
        print("  [观察] 第 2 轮仍调用了工具，来源表完整")
    print("  [判定] ✅ 该边界已被检测机制覆盖（本用例只观察，不要求特定结果）")
    return True


# ============================================================
async def main() -> None:
    print("=" * 64)
    print("D11 验证：引用定位（回答里的 [n] 能映射回原文来源）")
    print("=" * 64)

    results: list[tuple[str, bool]] = []

    print("\n########## A 层：纯逻辑（不需要 .env / 数据库） ##########")
    results.append(("A1 编号递增", case_a1_offset_increment()))
    results.append(("A2 越界检测", case_a2_check_citations()))
    results.append(("A3 并发隔离", await case_a3_concurrent_isolation()))

    print("\n########## B 层：端到端（需要 .env + Docker） ##########")
    try:
        await prepare_kb()
        results.append(("B1 引用可定位", await case_b1_answer_with_citation()))
        results.append(("B2 编号递增", await case_b2_multi_retrieval_in_one_request()))
        results.append(("B3 并发不串", await case_b3_concurrent_sessions()))
        results.append(("B4 重复提问边界", await case_b4_repeat_question()))
    except Exception as e:  # noqa: BLE001
        print(f"\n  ⚠️ B 层未跑通：{type(e).__name__}: {e}")
        print("     若是读取 .env 被拦，请在终端直接跑本脚本")

    section("汇总")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("\n完成。回答里的 [1] 现在能通过 sources 映射回原文了。")


if __name__ == "__main__":
    asyncio.run(main())
