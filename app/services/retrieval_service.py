"""
检索服务（RAG 核心：切片 + 入库 + 向量检索）
=============================================
RAG 的两阶段都在这：
- 离线建库：add_documents()  文本 → 切片 → 向量化 → 存 pgvector
- 在线检索：search()          问题 → 向量化 → 相似度排序 → top-k 片段

端到端流程（用户提问时）：
  search(问题)
    → embed_query(问题)             问题变向量
    → SQL: ORDER BY embedding <=> 问题向量   pgvector 算余弦距离并排序
    → 取 top-k 片段返回
    → 调用方把片段拼进 prompt 给 LLM
"""

from sqlalchemy import delete, select

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document import Document
from app.services.embedding_service import embed_query, embed_texts

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


async def add_documents(
    texts: list[str],
    source: str | None = None,
    chunk: bool = True,
) -> int:
    """
    文档入库：切片 → 向量化 → 存 pgvector。

    Args:
        texts: 原始文本列表（每个元素是一篇文档/一段话）
        source: 来源标识（文件名等），便于按来源过滤或删除
        chunk: 是否切片（短文本可传 False）
    Returns:
        实际入库的切片数
    """
    # ① 切片
    chunks: list[str] = []
    for text in texts:
        chunks.extend(split_text(text) if chunk else [text])

    if not chunks:
        return 0

    # ② 批量向量化（一次调用比逐条快得多）
    vectors = await embed_texts(chunks)

    # ③ 写入数据库
    async with async_session_factory() as session:
        for idx, (content, vec) in enumerate(zip(chunks, vectors, strict=True)):
            session.add(
                Document(
                    content=content,
                    embedding=vec,
                    source=source,
                    doc_metadata={"chunk_index": idx, "total": len(chunks)},
                )
            )
        await session.commit()

    logger.info("入库完成 source=%s 切片数=%d", source, len(chunks))
    return len(chunks)


async def search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """
    向量检索：找与问题语义最相近的 top-k 片段。

    SQL 核心：ORDER BY embedding <=> query_vector
      <=> 是 pgvector 的**余弦距离**算子（值越小越相似）
      相似度 = 1 - 距离（转成"越大越相似"更好理解）

    Args:
        query: 用户问题
        top_k: 返回条数
    Returns:
        [{"content": ..., "similarity": 0.83, "source": ...}, ...]（按相似度降序）
    """
    query_vec = await embed_query(query)

    async with async_session_factory() as session:
        # 用 SQLAlchemy 表达 pgvector 的距离排序
        distance = Document.embedding.cosine_distance(query_vec)
        stmt = (
            select(
                Document.content,
                Document.source,
                distance.label("distance"),
            )
            .order_by(distance)          # 距离升序 = 相似度降序
            .limit(top_k)
        )
        rows = (await session.execute(stmt)).all()

    results = [
        {
            "content": row.content,
            "source": row.source,
            "similarity": round(1 - row.distance, 4),   # 距离 → 相似度
        }
        for row in rows
    ]
    logger.info("检索完成 query=%r 命中=%d 条", query[:20], len(results))
    return results


async def clear_documents(source: str | None = None) -> int:
    """
    清空文档（指定 source 则只清该来源）。

    用途：换 embedding 模型后必须重建索引时，先清空再重新入库。
    """
    async with async_session_factory() as session:
        stmt = delete(Document)
        if source:
            stmt = stmt.where(Document.source == source)
        result = await session.execute(stmt)
        await session.commit()
    count = result.rowcount or 0
    logger.info("已清空文档 source=%s 删除数=%d", source or "(全部)", count)
    return count
