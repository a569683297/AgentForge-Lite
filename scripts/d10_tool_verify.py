"""
D10 验证脚本：检索工具化 + 同步/异步分流
==========================================
验证四件事：
1. search_documents 已注册（LLM 通过 get_tools_schema 能看到）
2. 同步工具 current_time 不回归（工具契约没被破坏）
3. **核心**：execute_tool 能正确 await 异步工具，而不是把 coroutine 对象
   str() 之后当成结果返回
4. 反例演示：不改注册表会拿到什么东西（肉眼看清危害）

用法：uv run python -m scripts.d10_tool_verify
"""

import asyncio

from app.services.retrieval_service import add_documents, clear_documents
from app.tools import execute_tool, get_tools_schema, list_tools

DOCS = [
    "公司年假政策：员工入职满一年后，每年可享受五天带薪休假。"
    "满三年后增加到十天，满五年后增加到十五天。请假需提前三个工作日申请。",
    "报销流程说明：员工出差产生的交通费、住宿费需在行程结束后十个工作日内提交报销单，"
    "需附上发票原件和出差审批单，财务在收到后五个工作日内完成审核打款。",
]


async def case_registry() -> None:
    """用例 1：工具已注册，且 schema 进了发给 LLM 的清单。"""
    print(f"\n{'='*60}\n用例 1：注册表检查\n{'='*60}")
    tools = list_tools()
    print(f"已注册工具：{tools}")
    print("✅ search_documents 已注册" if "search_documents" in tools else "❌ 未注册")

    schema = get_tools_schema()
    print(f"\n发给 LLM 的工具清单共 {len(schema)} 个：")
    for item in schema:
        fn = item["function"]
        print(f"  - {fn['name']}: {fn['description'][:40]}...")
        print(f"    参数：{list(fn['parameters']['properties'].keys())}")


async def case_sync_tool() -> None:
    """用例 2：同步工具不回归。"""
    print(f"\n{'='*60}\n用例 2：同步工具（current_time）不回归\n{'='*60}")
    result = await execute_tool("current_time", {})
    print(f"返回：{result!r}")
    ok = result.count(":") == 2 and "coroutine" not in result
    print("✅ 同步工具正常返回字符串" if ok else "❌ 同步工具异常")


async def case_async_tool() -> None:
    """用例 3（核心）：异步工具被正确 await。"""
    print(f"\n{'='*60}\n用例 3：异步工具（search_documents）分流\n{'='*60}")
    result = await execute_tool("search_documents", {"query": "出差的费用怎么报"})
    print("返回内容：")
    print("-" * 60)
    print(result)
    print("-" * 60)
    ok = "coroutine" not in result and "[1]" in result
    print("✅ 异步工具被正确 await，返回格式化文本" if ok else "❌ 拿到了 coroutine 对象")


async def case_bad_demo() -> None:
    """用例 4：反例——不 await 会拿到什么。"""
    print(f"\n{'='*60}\n用例 4：反例演示（若注册表不改会怎样）\n{'='*60}")

    async def fake_search(query: str) -> str:
        return f"「{query}」的真实检索结果"

    bad = fake_search("出差")          # 故意漏掉 await
    print(f"漏 await 的返回值类型：{type(bad).__name__}")
    print(f"str() 之后喂给 LLM 的内容：{str(bad)}")
    bad.close()                        # 手动关闭，避免 RuntimeWarning 干扰输出
    print("\n→ LLM 拿到的是这串内存地址，而不是检索结果；且程序不报错。")


async def main() -> None:
    await case_registry()
    await case_sync_tool()

    print(f"\n{'='*60}\n准备数据（清空并重新入库）\n{'='*60}")
    await clear_documents()
    n = await add_documents(DOCS, source="d10-verify")
    print(f"✅ 入库 {len(DOCS)} 篇 → {n} 个切片")

    await case_async_tool()
    await case_bad_demo()
    print(f"\n{'='*60}\n全部用例完成。\n{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
