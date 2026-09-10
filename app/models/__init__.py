"""
ORM 模型统一导出
================
所有模型在这里 import 一次，确保它们注册到 Base.metadata。
建表脚本依赖 Base.metadata.create_all 遍历所有继承 Base 的模型，
所以这里必须全部 import 到（否则表不会被创建）。
"""

from app.models.document import Document
from app.models.message import Message
from app.models.session import Session

__all__ = ["Session", "Message", "Document"]
