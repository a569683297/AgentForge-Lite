"""
D10 端到端验证：Agent 是否自己决定调用检索工具
================================================
验证目标（D10 验收标准）：
1. 用户问「只有文档里才有答案」的问题 → Agent 自动调用 search_documents
2. 回答内容来自文档，而非模型自己编（用虚构事实验证）
3. 闲聊类问题 → 工具不被调用（description 里写了"不要用于寒暄"，验证它生效）

验证手法：脚本内 monkeypatch execute_tool 做"探针"记录调用，
不改生产代码，跑完即失效。

用法：uv run python -m scripts.d10_agent_rag_verify

⚠ 2026-09-28 改：清理范围自限（原先调 delete_all_documents() 清空全库，
   会删光 D21 的长期语料）。现在按 VERIFY_PREFIX 前缀删除。
"""

import asyncio
import uuid

import app.services.agent_service as agent_module
from app.services.agent_service import run_agent
from app.services.document_service import (
    count_documents_by_prefix,
    delete_documents_by_prefix,
    ingest_texts,
)

# 本脚本造的文档统一带此前缀 —— 既是"这批数据归我管"的标记，也是清理边界
VERIFY_PREFIX = "d10-agent-rag"

# 刻意使用「模型不可能知道的虚构事实」：
# 回答正确 = 确实查了文档，而不是模型编的。
DOCS = [
    "公司内部项目管理规定：代号为「北辰」的项目由基础架构组负责，"
    "代号为「鸿雁」的项目由算法平台组负责，两个项目组均直接向 CTO 汇报。"
    "项目代号变更需经技术委员会审批，审批周期为三个工作日。",
    "公司团建经费标准：每人每季度上限为 800 元，"
    "由部门助理统一申请，需附活动签到表与消费明细。",
]

# (问题, 期望出现在回答里的关键词, 是否期望触发工具)
CASES = [
    ("「鸿雁」这个项目是哪个组负责的？", "算法平台组", True),
    ("团建经费一个人一个季度最多多少钱？", "800", True),
    ("你好，你是谁？", None, False),
]

# ---- 探针：记录 Agent 实际调用了哪些工具 ----
calls: list[tuple[str, dict]] = []
_original_execute_tool = agent_module.execute_tool


async def _spy_execute_tool(name: str, arguments: dict) -> str:
    calls.append((name, arguments))
    return await _original_execute_tool(name, arguments)


agent_module.execute_tool = _spy_execute_tool


async def cleanup_corpus() -> None:
    """跑完把自己的语料收干净 —— 别给 D22/D23 的检索评测留下会命中的垃圾。"""
    await delete_documents_by_prefix(VERIFY_PREFIX)
    left = await count_documents_by_prefix(VERIFY_PREFIX)
    print(f"\n（已清理本脚本语料：{VERIFY_PREFIX}* 残留={left}）")
    if left:
        raise RuntimeError(f"清理不干净：{VERIFY_PREFIX}* 还剩 {left} 份")


async def main() -> None:
    print(f"{'='*64}\n准备知识库（只清本脚本自己的数据）\n{'='*64}")
    await delete_documents_by_prefix(VERIFY_PREFIX)
    _, n = await ingest_texts(DOCS, filename=VERIFY_PREFIX)
    print(f"✅ 入库 {len(DOCS)} 篇 → {n} 个切片\n")

    for index, (question, keyword, expect_tool) in enumerate(CASES, start=1):
        calls.clear()
        print(f"{'='*64}\n用例 {index}：{question}\n{'='*64}")

        # D11 起 run_agent 返回 ChatResult（answer + sources），不再是裸字符串
        # D14 起 session_id 要求 uuid.UUID（与 sessions.id / messages.session_id 同类型）
        result = await run_agent(session_id=uuid.uuid4(), user_input=question)
        answer = result.answer

        called = [name for name, _ in calls]
        if calls:
            for name, args in calls:
                print(f"  工具调用：{name}  参数={args}")
        else:
            print("  工具调用：（无）")

        print(f"  回答：{answer}")

        # --- 判定 ---
        tool_ok = bool(called) == expect_tool
        print(f"  [触发判定] {'✅' if tool_ok else '❌'} "
              f"期望{'调用' if expect_tool else '不调用'}工具，实际{called or '未调用'}")

        if keyword:
            hit = keyword in answer
            print(f"  [内容判定] {'✅' if hit else '❌'} "
                  f"回答{'包含' if hit else '未包含'}文档关键词「{keyword}」")
        print()

    await cleanup_corpus()
    print(f"{'='*64}\n完成。可到 Langfuse 查看本次 trace 的 tool 调用详情。\n{'='*64}")


if __name__ == "__main__":
    asyncio.run(main())
