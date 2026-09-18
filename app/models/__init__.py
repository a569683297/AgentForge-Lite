"""
ORM 模型统一导出
================
所有模型在这里 import 一次，确保它们注册到 Base.metadata。
建表脚本依赖 Base.metadata.create_all 遍历所有继承 Base 的模型，
所以这里必须全部 import 到（否则表不会被创建）。

D12：新增 DocumentChunk（document_chunks 表）。
两个模块必须都被导入 —— DocumentChunk 的外键指向 documents，
少导一个，create_all 建表就会失败。
"""

from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.message import Message
from app.models.session import Session

__all__ = ["Session", "Message", "Document", "DocumentStatus", "DocumentChunk"]
