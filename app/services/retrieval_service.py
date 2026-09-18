"""
检索服务（RAG 在线检索）
=========================
D12 拆表后本文件只管**读**：

    切片工具：split_text()   长文本 → 有重叠的片段（写路径也复用它）
    向量检索：search()        问题 → 向量 → 相似度排序 → top-k

入库（写）与文档状态管理已移到 app/services/document_service.py —— 按读写分职责。

端到端流程（用户提问时）：
  search(问题)
    → embed_query(问题)                          问题变向量
    → SQL: document_chunks JOIN documents
             WHERE documents.status = 'ready'    只检索已就绪的文档
             ORDER BY embedding <=> 问题向量       pgvector 余弦距离，越小越相似
             LIMIT top_k
    → 返回片段 + 文件名 + 页码                     供引用定位使用
    → 调用方拼进 prompt 给 LLM
"""

from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.embedding_service import embed_query

# ---- 切片配置 ----
CHUNK_SIZE = 300        # 每个切片的字符数
CHUNK_OVERLAP = 50      # 相邻切片重叠字符数（防止把一句话从中间切断）
DEFAULT_TOP_K = 3       # 默认检索返回条数


def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    把长文本切成有重叠的片段（Chunking）。

    为什么要重叠（overlap）：
      如果严格按 300 字切断，可能把一句话切成两半：
        "...北京是中国的" | "首都，人口众多..."
      两片单独看都语义不完整 → 检索到也帮不上 LLM
      重叠 50 字后，两片都包含完整句子

    关键几何关系：重叠 = chunk_size - step
      chunk_size 管「语义密度」，overlap 管「抗切断」，是两个独立的调优维度，
      所以代码里先算 step（循环实际迈多大步），再进循环。

    Args:
        text: 原始文本
        chunk_size: 每片字符数
        overlap: 相邻片重叠字符数（必须 < chunk_size）
    Returns:
        切片列表
    """
    if overlap >= chunk_size:
        raise ValueError("overlap 必须小于 chunk_size")

    text = text.strip()
    if len(text) <= chunk_size:
        return [text] if text else []

    chunks: list[str] = []
    step = chunk_size - overlap        # 每次前进的步长
    for start in range(0, len(text), step):
        chunk = text[start : start + chunk_size]
        if chunk.strip():
            chunks.append(chunk.strip())
        if start + chunk_size >= len(text):   # 已到末尾
            break
    return chunks


async def search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """
    向量检索：找与问题语义最相近的 top-k 片段。

    SQL 核心：ORDER BY embedding <=> query_vector
      <=> 是 pgvector 的**余弦距离**算子（值越小越相似）
      相似度 = 1 - 距离（转成「越大越相似」，好理解也好展示）

    为什么要 JOIN documents：
      文件名和文档状态在 documents 表里，切片表只存 document_id。
      顺带解决了「只检索已就绪文档」这件事 —— processing / failed 的文档
      切片要么残缺、要么本就不该存在，不过滤就可能命中半截内容。

    Args:
        query: 用户问题
        top_k: 返回条数
    Returns:
        [{"content":..., "source":..., "page_ref":..., "similarity":...}, ...]（按相似度降序）
    """
    # 两个阶段必须用同一个 embedding 模型：只有同一模型的向量才在同一语义空间
    query_vec = await embed_query(query)

    # 用 SQLAlchemy 表达 pgvector 的距离排序
    distance = DocumentChunk.embedding.cosine_distance(query_vec)
    stmt = (
        select(
            DocumentChunk.content,
            DocumentChunk.page_ref,
            Document.filename,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == DocumentStatus.READY)
        .order_by(distance)          # 距离升序 = 相似度降序
        .limit(top_k)
    )

    async with async_session_factory() as session:
        rows = (await session.execute(stmt)).all()

    results = [
        {
            "content": row.content,
            # 沿用 "source" 这个键名：工具层与引用收集器都按它取来源展示名
            "source": row.filename,
            "page_ref": row.page_ref,
            "similarity": round(1 - row.distance, 4),   # 距离 → 相似度
        }
        for row in rows
    ]
    logger.info("检索完成 query=%r 命中=%d 条", query[:20], len(results))
    return results
