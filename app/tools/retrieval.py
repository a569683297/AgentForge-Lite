"""
工具：search_documents
======================
D10：把 D9 的向量检索（retrieval_service.search）包装成 Agent 可调用的工具。

D9 交付的是一个「函数」：search() —— 只有读过代码的人主动写调用，它才生效。
D10 交付的是一个「工具」：LLM 通过 get_tools_schema() 看到它，自己决定何时调用。

两个本质差异（面试常问）：
1. 触发权：从"程序员决定"变成"LLM 决定"
2. 自我介绍：工具带 description + parameters schema 进 prompt，
   LLM 靠它判断"这题该不该查资料"——所以描述文案要当 prompt 来写，不是写注释。

文件名注意：本文件是 app/tools/retrieval.py（工具层），
底层实现是 app/services/retrieval_service.py（服务层），别搞混。

D11 变化：工具返回值**仍是 str**（契约不变），
但额外把原始结构化结果登记到请求级收集器（app/core/citation.py），
让回答里的 [1] 能被映射回原文 —— 双通道：
    文本通道  → 给 LLM 读（带 [n] 编号）
    结构通道  → 给前端用（source / content / similarity 三个字段）
"""

from app.core.citation import record_sources
from app.services.retrieval_service import search
from app.tools.registry import register


@register(
    name="search_documents",
    description=(
        "检索公司内部知识库（员工手册、财务制度、运维规范等内部文档）。"
        "当用户询问公司制度、流程、规范、政策、报销、休假、权限等内部信息时使用。"
        "不要用于寒暄、通用常识或编程问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "检索用的查询词，用用户关注的核心概念，"
                    "例如'出差报销流程'、'年假天数'、'代码上线规范'"
                ),
            }
        },
        "required": ["query"],
    },
)
async def search_documents(query: str) -> str:
    """
    检索知识库，把结果格式化成带编号的文本片段返回。

    为什么返回 str 而不是 list[dict]：
    - LLM 只认文本，工具契约要求字符串
    - 编号 [1] [2] 是 D11 引用定位的接口约定：
      LLM 看到编号才能在回答里写"根据 [1]"，
      前端才能把 [1] 映射回原文来源。
    """
    results = await search(query)

    # 检索为空时返回明确说明，不能返回空字符串——
    # 空字符串会让 LLM 以为"查了但没结果"而开始编造答案。
    if not results:
        return f"未在知识库中找到与「{query}」相关的内容。"

    # ① 先登记结构化结果（D11 结构通道）。
    #    必须写在格式化**之前**：一旦 return 出去，results 这个结构就出不了这个函数了。
    #    返回的 offset 是本批之前的已有条数，用于让编号在多轮检索间全局递增。
    offset = record_sources(results)

    # ② 再格式化成文本（D11 文本通道，给 LLM 读）
    blocks: list[str] = []
    for index, item in enumerate(results, start=offset + 1):
        source = item.get("source") or "未知来源"
        # D12：PDF 类文档带上页码，LLM 写引用时能写到「第几页」这一级
        page = f" {item['page_ref']}" if item.get("page_ref") else ""
        blocks.append(
            f"[{index}] 来源：{source}{page}（相关度 {item['similarity']}）\n{item['content']}"
        )

    # 空行分隔，LLM 更容易分清"片段的边界"
    return "\n\n".join(blocks)
