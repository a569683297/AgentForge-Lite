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
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, Uuid, func
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

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        comment="文档ID（UUID，不可枚举——自增ID会泄露文档总数、也方便被遍历猜测）",
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        comment="原始文件名（展示用；不再是身份标识，重名也不会互相影响）",
    )
    file_type: Mapped[str] = mapped_column(
        String(16),
        comment="文件类型：pdf/docx/md/txt",
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
        comment="上传时间",
    )

    def __repr__(self) -> str:
        return f"<Document {self.filename} status={self.status} chunks={self.chunk_count}>"
