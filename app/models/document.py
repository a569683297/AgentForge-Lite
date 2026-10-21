"""
文档模型（documents 表）—— 文档级实体
======================================
一行 = 一份上传的文档（**不是切片**）。

D12 拆表背景：
  旧版把 documents 当切片表用（一行一个切片），三个后果：
  ① filename / status / chunk_count 这些「文档级」字段被迫摊到每一行
  ② chunk_count 是聚合量，在切片表里根本没有落脚点
  ③ 删除靠 source 字符串匹配 → 删不干净（名字对不上）/ 误删（两个同名文档）

  现在拆成两级：
      documents        1 行  = 1 份文档
      document_chunks  N 行  = N 个切片（外键 + ON DELETE CASCADE）

  级联交给**数据库**做（ON DELETE CASCADE），不在 ORM 里配 relationship：
  删文档 = 一条 DELETE，PostgreSQL 自己把切片带走，不会留孤儿数据。

账号体系（2026-10-21）：新增 `owner_id`，并且**去重指纹的唯一性范围收窄到人**。
  原来 `uq_documents_content_hash` 是**全库唯一** —— 那意味着 B 用户上传一份
  A 已经传过的文件，会被判为"重复文档"直接复用 A 的行：B 拿到 A 的文档 id、
  A 的切片、A 的引用。多租户下这条约束必须变成 `(owner_id, content_hash)`。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DocumentStatus:
    """
    文档入库状态常量。

    为什么用普通类装字符串常量，而不是 Enum：
    - 数据库侧用 String：加一个新状态不需要 ALTER TYPE（PostgreSQL 改 enum 很麻烦）
    - Python 侧用常量类：写 DocumentStatus.READY 而不是散落的 "ready" 字面量，
      拼错时 IDE 会提示，也省掉 Enum ↔ 字符串的来回转换
    """

    PROCESSING = "processing"   # 文件已收到，后台正在解析 / 切片 / 向量化
    READY = "ready"             # 全部切片已入库，可以被检索到
    FAILED = "failed"           # 中途出错，原因见 error_message


class Document(Base):
    """一份上传的文档（及其入库状态）。"""

    __tablename__ = "documents"

    # 内容指纹唯一索引：幂等去重的**最后一道防线**。
    # 应用层的「先查再插」有一个时间窗（SELECT 与 INSERT 之间），
    # 双击 / 双标签页 / 网关重试 / 多 worker 都会从那个窗口钻进来。
    # 唯一索引让数据库来判：第二个 INSERT 抛 IntegrityError，物理上不可能重复。
    #
    # ⚠ 范围是 **(owner_id, content_hash)**，不是 content_hash 单列 ——
    #   两个人各传一份同样的员工手册，是两份互不相干的文档。
    #   迁移脚本负责把旧的全局唯一索引删掉（`uq_documents_content_hash`）。
    #
    # ⚠ PostgreSQL 的唯一索引允许多个 NULL，所以历史数据（没有 hash）之间不会互相冲突。
    #   代价是它们不参与去重 —— 原始文件从没存过，hash 补不出来，这一点绕不过去。
    __table_args__ = (
        Index("uq_documents_owner_hash", "owner_id", "content_hash", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        comment="文档ID（UUID，不可枚举——自增ID会泄露文档总数、也方便被遍历猜测）",
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        # 用 SQLAlchemy 2.0 的通用 `Uuid`（本文件 id 列也是它），不引 PG 方言类型：
        # 两者在 PostgreSQL 上都落成 uuid，而 `Uuid` 不绑方言，测试换库时不用改模型。
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="归属用户；检索时按它过滤，缺了它别人能命中你的文档",
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        comment="原始文件名（展示用；不再是身份标识，重名也不会互相影响）",
    )
    file_type: Mapped[str] = mapped_column(
        String(16),
        comment="文件类型：pdf/docx/md/txt",
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "内容指纹 = sha256(原始文件字节) 的十六进制串（64 位）。"
            "算原始字节而不是解析后文本：解析可能失败（坏 PDF/扫描件），"
            "失败时算不出文本指纹，那些记录就永远无法参与去重；"
            "而且解析器升级会让同一文件的文本指纹漂移。"
            "NULL = 本字段上线前的历史数据（原始文件未留存，无法回填）"
        ),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=DocumentStatus.PROCESSING,
        server_default=DocumentStatus.PROCESSING,
        index=True,
        comment="processing/ready/failed（前端靠它显示「处理中/可检索/失败」）",
    )
    chunk_count: Mapped[int] = mapped_column(
        default=0,
        server_default="0",
        comment="切片数，入库成功（ready）时回填",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="失败原因（status=failed 时）。不记原因，这个状态就只是个开天窗的标记",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="首次上传时间（重试不改写它——那是历史事实）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment=(
            "最后一次状态变更时间。"
            "processing 卡死没有出口，靠它判断「多久没动静了」→ 超时回收（块 2）"
        ),
    )

    def __repr__(self) -> str:
        return f"<Document {self.filename} status={self.status} chunks={self.chunk_count}>"
