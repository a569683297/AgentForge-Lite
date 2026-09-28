"""
ORM 模型统一导出
================
所有模型在这里 import 一次，确保它们注册到 Base.metadata。
建表脚本依赖 Base.metadata.create_all 遍历所有继承 Base 的模型，
所以这里必须全部 import 到（否则表不会被创建）。

D12：新增 DocumentChunk（document_chunks 表）。
两个模块必须都被导入 —— DocumentChunk 的外键指向 documents，
少导一个，create_all 建表就会失败。

D21：新增 EvalCase / EvalRun（eval_cases / eval_runs 表）。
⚠ 这两张表 PRD 写作"原有表（不变）"，但实测库里**从来没有**它们 ——
少导这两个模型，create_all 就不会建表，`/api/eval/cases` 会直接 500。
"""

from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.eval import (
    CATEGORY_CROSS_DOC,
    CATEGORY_DOC_QA,
    CATEGORY_TOOL_CALL,
    DIFFICULTIES,
    EVAL_CATEGORIES,
    EXPECTED_TOOL_NOT_CALLED,
    EvalCase,
    EvalRun,
)
from app.models.message import Message
from app.models.session import Session

__all__ = [
    "Session",
    "Message",
    "Document",
    "DocumentStatus",
    "DocumentChunk",
    "EvalCase",
    "EvalRun",
    "EVAL_CATEGORIES",
    "DIFFICULTIES",
    "CATEGORY_DOC_QA",
    "CATEGORY_CROSS_DOC",
    "CATEGORY_TOOL_CALL",
    "EXPECTED_TOOL_NOT_CALLED",
]
