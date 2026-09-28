"""
评测模型（D21）—— 评测集与评测运行
====================================
两张表：
    eval_cases   一行 = 一条评测用例（题面 + 参考答案 + 判定依据 + 标注）
    eval_runs    一行 = 一次评测运行（配置 + 汇总分 + 用哪版评测集）

⚠ 一个必须先说清的事实（D21 实测）：
    PRD §10 把这两张表写作「原有表（不变）……定义见 docs/PRD.md §10」，
    但**数据库里从来没有它们** —— 建表前 `information_schema.tables` 只有
    documents / document_chunks / messages / sessions 四张。
    这是"文档认为已存在、实际从未实现"的静默缺口：不查库就发现不了，
    而 `/api/eval/cases` 这条验收点会直接 500。所以 D21 的隐藏工作量是**先建表**。

--------------------------------------------------------------------------
为什么字段比 PRD 多了五个（不是随意加的，每个都对应一条纪律）
--------------------------------------------------------------------------
    case_key      稳定业务键（A01/B03…）
                  bigserial 的 id 每次重建都会变，"第 5 条题"这个说法不可靠；
                  指纹、报告、失败案例定位都需要一个**不随重建变化**的名字
    evidence      判定依据（原文片段 / [不存在] / [工具]）
                  没有它就无法机检"参考答案是不是编的"、"负例是不是真的没有答案"
    doc_slugs     依据文档的**业务名**（annual-leave / infosec…）
                  ⚠ 指纹只能用它，不能用 doc_ids —— 见 d21_seed.py 的说明
    is_negative   是否负例（正确行为是拒答）
                  不能靠人去读 reference 判断，程序必须能直接识别
    difficulty    预设难度（easy/medium/hard）
                  D22/D23 要做**分层抽样**和"分类别出分"，得先有这个标注

--------------------------------------------------------------------------
为什么 id 用 BIGSERIAL 而 documents 用 UUID
--------------------------------------------------------------------------
不是前后不一致：自增 ID 的风险是"可枚举、泄露总量"，那是**面向用户的数据**才需要防的。
评测集是内部研发资料，且报告、命令行、复现说明里大量要写"第几条题"——
BIGSERIAL 让这些地方能写 `5` 而不是 `3f2a…-uuid`。取舍是**可读性优先**。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# ---- 类别常量（与 PRD F7.1 的三类一一对应）----
CATEGORY_DOC_QA = "doc_qa"          # A 类：单文档问答（含负例）
CATEGORY_CROSS_DOC = "cross_doc"    # B 类：跨文档推理
CATEGORY_TOOL_CALL = "tool_call"    # C 类：工具调用

EVAL_CATEGORIES: tuple[str, ...] = (CATEGORY_DOC_QA, CATEGORY_CROSS_DOC, CATEGORY_TOOL_CALL)

# ---- 难度常量（D22 分层抽样用）----
DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")

# ---- expected_tool 的哨兵值 ----
# 为什么不用 NULL 表示"不该调用任何工具"：
#   NULL 在别处表示"这个字段没填"（例如 A/B 类题不涉及工具）。两者语义完全不同，
#   混用会让"该不该调工具"变成需要靠 category 二次推断的隐式信息。
#   同族问题见 D19：`similarity=None` 被当成"相关度 0"渲染，都是一处字段承担两种含义。
EXPECTED_TOOL_NOT_CALLED = "__none__"


class EvalCase(Base):
    """一条评测用例。"""

    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="自增主键（内部资料，可读性优先于防枚举）",
    )
    case_key: Mapped[str] = mapped_column(
        String(16),
        unique=True,
        index=True,
        comment="稳定业务键（A01/B03/C10）—— 报告与失败案例定位用它，不用会变的 id",
    )
    category: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="doc_qa=单文档问答 / cross_doc=跨文档推理 / tool_call=工具调用",
    )
    question: Mapped[str] = mapped_column(
        Text,
        comment="题面（喂给被测系统的原始提问）",
    )
    reference: Mapped[str] = mapped_column(
        Text,
        comment="参考答案 —— judge 判分时的标尺；负例写成「知识库中没有…」这类可判定陈述",
    )
    evidence: Mapped[str] = mapped_column(
        Text,
        comment="判定依据：原文片段 / 「[不存在] 关键词」/「[工具] 名称」。由验证脚本机检",
    )
    doc_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(Uuid),
        nullable=True,
        comment="依据文档的 UUID（出题自检 + 事后失败归因用）。"
                "⚠ 绝不可作为检索过滤条件 —— 那等于开卷考试还给页码，A/B/C 的差异会被抹平",
    )
    doc_slugs: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        server_default="{}",
        comment="依据文档的业务名（annual-leave 等）。指纹只用它，不用 doc_ids",
    )
    expected_tool: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="工具类题期望调用的工具名；'__none__' 表示显式要求不调用任何工具",
    )
    is_negative: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        comment="负例（语料中无答案，正确行为是拒答）—— 用于测幻觉控制",
    )
    difficulty: Mapped[str] = mapped_column(
        String(8),
        comment="预设难度 easy/medium/hard（D22 分层抽样与分类别归因用）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="写入时间",
    )

    def __repr__(self) -> str:
        return f"<EvalCase {self.case_key} {self.category} neg={self.is_negative}>"


class EvalRun(Base):
    """
    一次评测运行（D22 起写入）。

    D21 先建好空表：`/api/eval/cases` 不需要它，但报告要能回答
    「这个分数是哪版评测集、哪个 judge、重复几次跑出来的」——
    这三个问题的答案必须是**数据里带的**，不能靠事后回忆或翻日志
    （同 D19 的教训：降级必须可归因，否则降级样本会被算成正常成绩）。
    """

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="pure_vector / hybrid / hybrid_rerank（消融的自变量，只允许改这一个）",
    )
    dataset_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="本次运行所用评测集的内容指纹（sha256）—— 报告必须能证明用的是哪一版题",
    )
    judge_model: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="打分用的 judge 模型。不写清楚，跨 judge 的分数会被当成可比的",
    )
    runs_per_case: Mapped[int | None] = mapped_column(
        comment="每条题重复打分次数（D20 定的 3 次多数投票）—— 决定噪声门槛，必须随分数一起报",
    )
    score_correctness: Mapped[float | None] = mapped_column(Float, nullable=True, comment="正确性均分")
    score_faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True, comment="引用忠实度均分")
    score_completeness: Mapped[float | None] = mapped_column(Float, nullable=True, comment="完整性均分")
    accuracy: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="二值化准确率（correctness ≥ 4 记对）—— 分母口径必须在报告里写明",
    )
    report_path: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="报告文件路径")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<EvalRun {self.config_name} acc={self.accuracy}>"
