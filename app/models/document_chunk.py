"""
文档切片模型（document_chunks 表）—— 切片级实体
================================================
一行 = 一个切片 + 它的向量。这是 RAG 检索的最小单元。

与 documents 的关系：
    documents.id  ←──  document_chunks.document_id
                       外键 ondelete="CASCADE"

    删一份文档 → PostgreSQL 自动删掉它的全部切片，不会留下孤儿数据。
    这是拆表在「删除」上的核心收益：从字符串匹配（`WHERE source='员工手册.md'`）
    变成主键级联（`WHERE document_id = <uuid>`），既删得干净也不会误删同名文档。

⚠️ 维度与 embedding 模型绑定（迁移注意）：
- 本表 embedding 维度 = settings.embedding_dim（当前 bge-small-zh-v1.5 = 512）
- **换 embedding 模型若维度变化，本表必须重建**（列类型 vector(N) 不兼容）
- 即使维度相同，不同模型的向量空间也不可比 → 换模型 = 全量 reindex
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.core.db import Base


class DocumentChunk(Base):
    """一个文档切片及其向量。"""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="切片自增ID",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属文档；删文档时数据库级联删除本行",
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="在文档内的切片序号（从 0 开始），用于展示切片顺序",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="切片文本（检索命中后拼进 prompt 的就是它）",
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dim),
        comment=f"文本向量（维度由 embedding 模型决定，当前 {settings.embedding_dim}）",
    )
    page_ref: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="页码引用（PDF 为 'p.3'；md/txt/docx 无页概念，为 NULL）——引用定位用",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="入库时间",
    )

    __table_args__ = (
        # 按文档查/删切片时走索引
        Index("idx_chunks_document", "document_id"),
        # 注：向量近似检索索引（HNSW/IVFFlat）在数据量上万后再建，
        # 小数据量下顺序扫描足够快，过早建索引反而增加写入成本
    )
