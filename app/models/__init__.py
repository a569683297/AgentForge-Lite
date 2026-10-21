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

D22：新增 EvalCaseResult（eval_case_results 表，评测明细）。
PRD §10 有 DDL，但实测库里同样**从未存在** —— 与 D21 是同一类缺口
（"文档认为已有、实际从未实现"），这是第二次。少导它，明细就无处落库，
而验收点"明细表有行"会以"表不存在"的形式直接报错。

工具上传：新增 ToolServer（tool_servers 表，用户上传的 MCP Server）。
少导它，create_all 不会建表，`/api/tool-servers` 会直接 500。

模型切换 S1：新增 LlmModel（llm_models 表，自定义模型注册表）。
只存"用户自己添加的"模型；内置模型由 .env 驱动、不入库（见 models/llm_model.py）。
少导它，create_all 不会建表，`/api/models` 会直接 500。

账号体系（2026-10-21）：新增 User（users 表）。
⚠ 下面所有带 `owner_id` 的业务表（sessions / documents / tool_servers / eval_runs /
  llm_models）都有指向 users.id 的外键。外键是按**字符串**声明的（`"users.id"`），
  在 `create_all` 生成 DDL 时才去 metadata 里找目标表 ——
  所以本文件里 import 的先后顺序无所谓，**但必须全部 import 到**：
  漏了 users，create_all 会直接抛 `NoReferencedTableError`。

用户画像（2026-10-21）：新增 UserProfile / UserProfileFact
（user_profiles / user_profile_facts 两张表）。
它们是**跨会话记忆**的载体：显式字段一人一行，隐式事实一条一行。
少导它们，`/api/profile` 会直接 500，而对话链路的画像注入会静默变成空
（`profile_service.get_profile` 是 fail-open 的 —— 表不存在只会记一条 warning，
对话照常进行，只是永远没有个性化。这类"降级得太安静"的故障正是要防的）。
"""

from app.models.audit_log import ACTIONS, AuditLog
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.eval import (
    CATEGORY_CROSS_DOC,
    CATEGORY_DOC_QA,
    CATEGORY_TOOL_CALL,
    DIFFICULTIES,
    EVAL_CATEGORIES,
    EXPECTED_TOOL_NOT_CALLED,
    FAILURE_REASONS,
    PASS_THRESHOLD,
    EvalCase,
    EvalCaseResult,
    EvalRun,
)
from app.models.llm_model import LlmModel
from app.models.tool_server import TOOL_SERVER_STATUSES, ToolServer
from app.models.message import Message
from app.models.session import Session
from app.models.user import ROLE_ADMIN, ROLE_USER, ROLES, User
from app.models.user_profile import (
    EXPLICIT_FIELDS,
    EXPLICIT_KEYS,
    FACT_FACT,
    FACT_GOAL,
    FACT_KINDS,
    FACT_PREFERENCE,
    UserProfile,
    UserProfileFact,
)

__all__ = [
    "Session",
    "Message",
    "Document",
    "DocumentStatus",
    "DocumentChunk",
    "EvalCase",
    "EvalRun",
    "EvalCaseResult",
    "LlmModel",
    "ToolServer",
    "TOOL_SERVER_STATUSES",
    "EVAL_CATEGORIES",
    "DIFFICULTIES",
    "CATEGORY_DOC_QA",
    "CATEGORY_CROSS_DOC",
    "CATEGORY_TOOL_CALL",
    "EXPECTED_TOOL_NOT_CALLED",
    "FAILURE_REASONS",
    "PASS_THRESHOLD",
    "User",
    "ROLES",
    "ROLE_USER",
    "ROLE_ADMIN",
    "AuditLog",
    "ACTIONS",
    # 用户画像（2026-10-21）
    "UserProfile",
    "UserProfileFact",
    "EXPLICIT_FIELDS",
    "EXPLICIT_KEYS",
    "FACT_KINDS",
    "FACT_PREFERENCE",
    "FACT_FACT",
    "FACT_GOAL",
]
