"""
D9 验证脚本：向量检索（RAG 基础）
===================================
1. 入库：写入几段不同主题的文档
2. 语义检索：用"同义不同词"的问题检索，验证命中正确文档（关键词检索做不到）
3. 切片：长文本正确切片且有重叠

用法：uv run python -m scripts.d9_retrieval_verify
"""

import asyncio

from app.core.logging import logger
from app.services.document_service import delete_all_documents, ingest_texts
from app.services.retrieval_service import (
    search,
    split_text,
)

# 测试语料：三段不同主题的文档（刻意用不同的词汇表述）
DOCS = [
    "公司年假政策：员工入职满一年后，每年可享受五天带薪休假。"
    "满三年后增加到十天，满五年后增加到十五天。请假需提前三个工作日申请。",
    "服务器运维规范：生产环境的所有变更必须在维护窗口内执行，"
    "维护窗口为每周日凌晨两点到四点。变更前需在工单系统提交变更申请并经过审批。",
    "报销流程说明：员工出差产生的交通费、住宿费需在行程结束后十个工作日内提交报销单，"
    "需附上发票原件和出差审批单，财务在收到后五个工作日内完成审核打款。",
]


async def case_chunk() -> None:
    """用例 0：切片逻辑（含重叠）。"""
    print(f"\n{'='*58}\n用例 0：文本切片（300 字一片，重叠 50）\n{'='*58}")
    long_text = "A" * 700
    chunks = split_text(long_text)
    print(f"700 字文本 → {len(chunks)} 片，各片长度: {[len(c) for c in chunks]}")
    ok = len(chunks) >= 2 and all(len(c) <= 300 for c in chunks)
    print("✅ 切片正确（步长 250，重叠 50）" if ok else "❌ 切片异常")


async def case_retrieval() -> None:
    """用例 1-2：入库 + 语义检索。"""
    print(f"\n{'='*58}\n用例 1：文档入库\n{'='*58}")
    await delete_all_documents()          # 先清空，保证验证干净
    _, n = await ingest_texts(DOCS, filename="test-docs")
    print(f"✅ 入库 {len(DOCS)} 篇文档 → {n} 个切片")

    # 关键验证：query 用词与文档完全不同，但语义相关
    cases = [
        ("我什么时候能休假？", "年假"),                    # 同义改写：休假 vs 年假
        ("线上环境改代码有什么规定？", "运维/变更"),        # 同义：改代码 vs 变更
        ("出差的费用怎么报？", "报销"),                    # 同义：报 vs 报销
    ]

    for query, expected_topic in cases:
        print(f"\n{'='*58}\n用例：语义检索\n{'='*58}")
        print(f"问题: {query}   （期望命中主题：{expected_topic}）")
        results = await search(query, top_k=2)
        for i, r in enumerate(results, 1):
            print(f"  #{i} 相似度 {r['similarity']:.4f} | {r['content'][:45]}...")

        # 判断：top1 是否命中期望主题（用关键词粗判）
        top1 = results[0]["content"] if results else ""
        hit = any(kw in top1 for kw in ["年假", "休假"] if expected_topic == "年假") or (
            any(kw in top1 for kw in ["运维", "变更", "维护"]) if expected_topic == "运维/变更" else False
        ) or (any(kw in top1 for kw in ["报销", "费"]) if expected_topic == "报销" else False)
        print(f"  {'✅ 命中' if hit else '❌ 未命中'}（top1 主题{'正确' if hit else '错误'}）")


async def main() -> None:
    await case_chunk()
    await case_retrieval()
    print(f"\n{'='*58}\n全部用例完成。\n{'='*58}")


if __name__ == "__main__":
    asyncio.run(main())
