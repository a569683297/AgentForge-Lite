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

D16 新增两列 —— 关键词检索的「索引侧」：

    content_tokens   jieba 分词结果（空格分隔）。应用侧写入，见 services/tokenizer.py
    content_tsv      生成列：数据库从 content_tokens 派生 to_tsvector('simple', ...)
                     → 应用侧**永远不写它**，因此也永远不可能写歪

    为什么 content_tsv 要做生成列、而不是普通列：
        普通列要求每次写入都记得同步更新，漏一次就是静默不一致
        （倒排索引里的 token 与 content_tokens 对不上，检索不出来但毫无报错）。
        生成列把「tsv 恒等于 to_tsvector(tokens)」交给数据库保证。

        ⚠️ 但它只能保证**第二段一致性**（tokens → tsv）。
           第一段（content → tokens）仍在应用侧，由分词器负责 ——
           这就是分词器必须是单点出口的原因（见 tokenizer.py）。

⚠️ 维度与 embedding 模型绑定（迁移注意）：
- 本表 embedding 维度 = settings.embedding_dim（当前 bge-small-zh-v1.5 = 512）
- **换 embedding 模型若维度变化，本表必须重建**（列类型 vector(N) 不兼容）
- 即使维度相同，不同模型的向量空间也不可比 → 换模型 = 全量 reindex
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
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
    content_tokens: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="jieba 分词结果（空格分隔）；NULL = 尚未分词，需跑 scripts/d16_migrate.py 回填",
    )
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', content_tokens)", persisted=True),
        nullable=True,
        comment="tsvector 倒排（生成列，数据库自动从 content_tokens 派生，应用侧不写）",
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
        # D16：倒排索引。关键词检索靠 `content_tsv @@ tsquery` 走它筛候选，避免全表扫描。
        #      GIN 是倒排表的标准索引类型；建在生成列上，内容随 content_tokens
        #      自动维护，不需要应用侧干预。
        Index("idx_chunks_tsv", "content_tsv", postgresql_using="gin"),
        # 注：向量近似检索索引（HNSW/IVFFlat）在数据量上万后再建，
        # 小数据量下顺序扫描足够快，过早建索引反而增加写入成本
    )
