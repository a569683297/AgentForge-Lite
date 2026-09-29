"""
评测模型（D21 起）—— 评测集、评测运行、评测明细
================================================
三张表构成"汇总 ↔ 明细"的两级结构：

    eval_cases         一行 = 一条评测用例（题面 + 参考答案 + 判定依据 + 标注）
    eval_runs          一行 = 一次评测运行（配置 + 汇总分 + 用哪版题、哪版语料）
    eval_case_results  一行 = 某次运行里、某道题的**某一次生成**及其打分结果   ← D22 新增

为什么明细必须单独一张表（PRD F7.10）：

    汇总分不可再下钻。eval_runs 只有三个维度均分和准确率，要回答
    「哪类题失分最多」——答不出来。而 PRD §9.5 明确要求报告有"每类明细"
    与"失败案例各取 3 条"，没有单题明细，这两项都产不出来。
    它还是 Agentic BI（场景 F）下钻链的第 2 跳：
        `SELECT category, AVG(score_faithfulness) ... GROUP BY category`
    类表：eval_runs 是"班级平均分"，eval_case_results 是"每个学生每道题的得分"。

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

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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

# ---- 二值化阈值（PRD F7.5）----
# correctness ≥ 4 记"对"。为什么不用平均分：
#   judge 的分数有 ±0.3 量级的抖动（D20 实测），而 4.02 与 3.98 的差别比抖动还小，
#   报平均分等于在比较噪声。二值化把"分数差"换成"对/错差"，噪声才可比。
PASS_THRESHOLD = 4

# ---- 失败归因枚举（D22 新增，PRD §17 待细化项第 3 条要求在此定义）----
# 为什么是**固定枚举**而不是让 judge 自由写一句话：
#   ① 同义异形：5 次调用会写出"检索未命中"/"资料缺失"/"没找到"→ 聚类时算成 4 类，
#      报告里的失败模式分布是**假的**
#   ② 不可复现：同一份答案重跑一次，judge 换一种措辞 → 同一份报告两次不一样
#   ③ 不可测：自由文本没法写断言（"这条应该是 hallucination"无法机检）
# 同族先例：D14 的 messages.tool_calls 被存成 JSON 字面量 null 而非 SQL NULL，
#   同一语义有两种表示 → 整片逻辑静默失效。
# 正解：judge 只输出**数字**（那是它不可替代的语义判断），本字段由**纯函数**推导
#   （见 app/services/failure_taxonomy.py，可单测、可复算）。
FAILURE_SYSTEM_ERROR = "system_error"        # 跑系统阶段就失败（LLM 全通道挂 / 超时 / 异常）
FAILURE_TOOL_MISS = "tool_miss"              # 工具类题：该调没调，或调了别的
FAILURE_HALLUCINATION = "hallucination"      # 编造：答案里有片段中没有的事实
FAILURE_INCOMPLETE = "incomplete"            # 答不全：漏掉关键要点
FAILURE_WRONG_ANSWER = "wrong_answer"        # 答错：方向沾边但关键事实错

# 顺序 = 优先级（上游原因优先于下游表现）。"没调工具"是根因，"答错了"是它的结果；
# 若一律记成 wrong_answer，"工具选择错误"这个失败模式就被藏起来了 —— 而它恰恰是
# C 类题存在的意义。
FAILURE_REASONS: tuple[str, ...] = (
    FAILURE_SYSTEM_ERROR,
    FAILURE_TOOL_MISS,
    FAILURE_HALLUCINATION,
    FAILURE_INCOMPLETE,
    FAILURE_WRONG_ANSWER,
)


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
    corpus_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="本次运行所用**语料**的内容指纹（sha256，含切片方式）—— D22 新增。"
                "与 dataset_fingerprint 分开两列而不是合并：两者独立变化，"
                "合在一起时\"变了\"**无法归因**（是题被改了还是语料被换了？）",
    )
    judge_model: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="打分用的 judge 模型。不写清楚，跨 judge 的分数会被当成可比的",
    )
    generation_runs: Mapped[int | None] = mapped_column(
        comment="每条题重复**生成**次数（≠ runs_per_case）。D21 实测生成侧抖动 ≫ 裁判侧，"
                "所以这两个是必须分开的两个旋钮：重复打分救不了\"这次答案本身没生成好\"",
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


class EvalCaseResult(Base):
    """
    评测明细（D22 新增）—— 一行 = 某次运行里、某道题的**某一次生成**及其打分结果。

    ----------------------------------------------------------------------
    粒度为什么是"一次生成"而不是"一道题"
    ----------------------------------------------------------------------
    `generation_runs > 1` 时一道题会有多份答案。如果把它们压成一行：
        · 看不出这份题到底飘不飘（而这正是 D22 要量的东西）
        · 5 次打分只有一组原始分数，多个答案的分数会互相污染
    所以粒度取最细：**一道题生成 N 次 → N 行**。
    "这道题的结论"由聚合得出（`GROUP BY case_key` + 多数投票）；
    "生成侧抖动" = 同一个 case_key 的 N 行里 `passed` 的分布。
    一行只存一次生成，才能同时支持这两件事。

    ----------------------------------------------------------------------
    category 为什么冗余存一份（明明可以 join eval_cases）
    ----------------------------------------------------------------------
    PRD F7.10 的用法就是 `SELECT category, AVG(score_faithfulness) ... GROUP BY category`，
    这是报告的固定查询。冗余之后免掉 join，且写入时即冻结（评测是只追加的）。
    代价是"两处可能不一致" → 所以验证脚本有一条断言钉住：
    明细的 category 必须等于 eval_cases 的 category（d22_verify.py 的 C 段）。

    反之 **config_name 故意不冗余**：它是 run 的属性，一台 run 只有一个值，
    冗余反而多一处可能写歪的地方，而 join 一次的成本可忽略。

    ----------------------------------------------------------------------
    tool_calls / retrieved_chunks 为什么必须存
    ----------------------------------------------------------------------
    它们**不是分数**，而是"事后能否重新判定"的唯一依据：
        · tool_calls —— 算 tool_miss 的唯一输入（PRD §17：失败归因要能区分
          "答错"与"没调工具"，而这两者从答案文本里看不出来）
        · retrieved_chunks —— judge 的 faithfulness 维度是拿**片段**当对照物
          （不是拿参考答案），不存下来就没法复现或重判
    缺了它们，明细表就只剩一堆分数，失去下钻能力 —— 与只有 eval_runs 没本质区别。
    """

    __tablename__ = "eval_case_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("eval_runs.id", ondelete="CASCADE"),
        index=True,
        comment="所属评测运行；删 run 时数据库级联删明细（不留孤儿分数）",
    )
    case_key: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="题目业务键。**不建外键**：评测明细是历史记录，题目被重写/删除后，"
                "历史分数仍应可读（外键会拦住评测集重建，而那正是经常发生的事）",
    )
    category: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="冗余存一份，供 PRD F7.10 的按类别聚合免 join",
    )
    generation_index: Mapped[int] = mapped_column(
        Integer,
        comment="这是该题第几次生成（0 起）。generation_runs=1 时恒为 0",
    )
    answer: Mapped[str] = mapped_column(
        Text,
        comment="被测系统给出的答案原文（报告里\"失败案例各取 3 条\"直接取它）",
    )
    retrieved_chunks: Mapped[str] = mapped_column(
        Text,
        comment="本次检索到的片段（faithfulness 的对照物，也是复现/重判的依据）",
    )
    tool_calls: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
        comment="实际调用的工具名序列（按调用顺序）。算 tool_miss 的唯一输入。"
                "⚠ none_as_null=True：SQLAlchemy 的 JSON 默认把 None 存成 JSON 字面量 null，"
                "导致 `IS NULL` 恒为假（D14 踩过的坑）",
    )
    sources_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        comment="检索到的片段条数（0 说明检索完全没命中 —— 与\"命中但没用上\"是两回事）",
    )

    # ---- 打分结果（runs_per_case 次的聚合值）----
    score_correctness: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="正确性（多次打分取中位数）"
    )
    score_faithfulness: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="引用忠实度（中位数）"
    )
    score_completeness: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="完整性（中位数）"
    )
    judge_runs: Mapped[int] = mapped_column(
        Integer, comment="本行实际成功打分的次数（< runs_per_case 说明有调用失败）"
    )
    judge_raw: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
        comment="各次原始分数（审计用）。必须存：D20 的教训是\"报平均分等于在比噪声更小的差异\"，"
                "存下每次的分数才能事后算抖动、判断某个差异是否超出门槛",
    )

    # ---- 结论（由纯函数推导，不是 judge 写的）----
    passed: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        index=True,
        comment="是否通过。判据 = correctness ≥ 4（PRD F7.5），"
                "但 C 类题 tool_miss 有否决权 —— 见 failure_taxonomy.derive_case_outcome",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(32),
        index=True,
        nullable=True,
        comment=f"失败归因，取值见 FAILURE_REASONS={FAILURE_REASONS}；"
                "passed=True 时为 NULL（不是\"未填\"）",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<EvalCaseResult run={self.run_id} {self.case_key}#{self.generation_index} "
            f"passed={self.passed} reason={self.failure_reason}>"
        )
