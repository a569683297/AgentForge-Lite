"""
评测接口的数据结构（D21 起）
=============================
D21：评测集本身（EvalCase* / EvalDatasetOut）
D22：评测运行与明细（EvalRun* / EvalCaseResult*）

按 PRD §11 的定义，`GET /api/eval/cases` 的响应是 `[{id, category, question}]` 这个形状 ——
**不含参考答案**。这里把形状略微扩展（补上 case_key / difficulty / is_negative），
但**依然不含 reference / evidence**，理由：

    这两列是"考卷的答案页"。列表接口是最容易被顺手接进前端页面的地方，
    参考答案一旦在列表里返回，就很容易被展示出来 —— 而调试时人看到答案、
    或被测系统在某个环节读到答案，评测就**不再是盲测**了。
    要看答案请走单条详情接口（GET /api/eval/cases/{case_key}），那是一个**显式动作**。

这不是防攻击（内部资料，本来就不对外），是防手滑。

D22 的明细接口沿用同一条纪律的**反面**：明细里的 `answer` 是**被测系统的输出**，
不是答案页，可以返回；但 `retrieved_chunks`（几千字的检索片段）只在单条详情里给 ——
列表接口把它带上会让一次 50 条的查询返回几百 KB，而没人会那样用。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field


class EvalCaseOut(BaseModel):
    """列表里的单条用例（不含答案页）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="自增主键")
    case_key: str = Field(description="稳定业务键（A01/B03/C10）")
    category: str = Field(description="doc_qa / cross_doc / tool_call")
    difficulty: str = Field(description="预设难度 easy/medium/hard")
    is_negative: bool = Field(description="负例（语料中无答案，正确行为是拒答）")
    question: str = Field(description="题面")


class EvalCaseDetailOut(EvalCaseOut):
    """单条用例详情 —— 含参考答案与判定依据（`/api/eval/cases/{case_key}`）。"""

    reference: str = Field(description="参考答案（judge 判分的标尺）")
    evidence: str = Field(description="判定依据：原文片段 / [不存在] / [工具]")
    doc_slugs: list[str] = Field(description="依据文档的业务名")
    expected_tool: str | None = Field(
        default=None,
        description="工具类题期望调用的工具；'__none__' 表示显式要求不调用任何工具",
    )


class EvalDatasetOut(BaseModel):
    """
    评测集整体信息 —— **冻结的可验证出口**。

    为什么要有这个端点：指纹光算出来没用，必须有一个地方能**被外部读到**，
    才能用来对账（D23 报告里写的指纹，与这里读到的对不对得上是同一件事）。
    """

    total: int = Field(description="用例总数")
    negative: int = Field(description="其中负例条数")
    by_category: dict[str, int] = Field(description="按类别分布")
    by_difficulty: dict[str, int] = Field(description="按难度分布")
    covered_slugs: list[str] = Field(description="被题目覆盖到的语料 slug（可查是否有语料没被用到）")
    fingerprint: str = Field(description="内容指纹（sha256）—— 变了就说明评测集被改过")


# ============================================================
# D22：评测运行与明细
# ============================================================
class EvalRunOut(BaseModel):
    """
    一次评测运行（汇总）。

    这四个字段是**必须随分数一起报**的元信息 —— 少任何一个，这个分数都不可比：
        哪个配置（config_name）、哪版题（dataset_fingerprint）、
        哪版语料（corpus_fingerprint）、哪个 judge 打了几次（judge_model/runs_per_case）。
    "分数涨了"如果不是在同样这四个条件下测出来的，就不是同一个结论。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    config_name: str = Field(description="检索配置（消融的自变量）")
    dataset_fingerprint: str | None = Field(description="评测集指纹 —— 证明用的是哪一版题")
    corpus_fingerprint: str | None = Field(description="语料指纹 —— 证明用的是哪一版语料")
    judge_model: str | None = Field(description="打分用的 judge 通道")
    generation_runs: int | None = Field(description="每条题重复生成次数")
    runs_per_case: int | None = Field(description="每份答案重复打分次数")
    score_correctness: float | None = Field(description="正确性均分（分母是明细行数，非题数）")
    score_faithfulness: float | None = Field(description="引用忠实度均分")
    score_completeness: float | None = Field(description="完整性均分")
    accuracy: float | None = Field(description="准确率（分母是**题数**：每题多次生成先多数投票）")
    report_path: str | None = None
    created_at: datetime


class EvalCategoryStatOut(BaseModel):
    """按类别聚合的一行（PRD F7.10 的固定查询结果）。"""

    category: str
    rows: int = Field(description="明细行数（= 题数 × generation_runs）")
    cases: int = Field(description="题目条数")
    passed_rows: int = Field(description="通过的明细行数")
    avg_correctness: float | None = None
    avg_faithfulness: float | None = None
    avg_completeness: float | None = None


class EvalRunDetailOut(EvalRunOut):
    """运行详情：在汇总上补两份下钻数据（D24 报告要用的就是它们）。"""

    by_category: list[EvalCategoryStatOut] = Field(
        default_factory=list, description="按类别聚合 —— 回答\"哪类题失分最多\""
    )
    failure_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description="失败模式分布（failure_reason → 条数）—— 回答\"失败里有多少是没检索到、多少是编造\"",
    )
    rows: int = Field(default=0, description="明细总行数")
    generation_inconsistent: int = Field(
        default=0,
        description="多次生成结论不一致的题数（生成侧抖动的直接观测）——"
                    "这个数一高，说明分数里有一部分只是随机性，不是配置差异",
    )


class EvalCaseResultOut(BaseModel):
    """一条评测明细（不含 retrieved_chunks —— 那是详情接口的内容）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    case_key: str
    category: str
    generation_index: int = Field(description="第几次生成（0 起）")
    answer: str = Field(description="被测系统给出的答案")
    tool_calls: list[str] | None = Field(description="实际调用的工具序列（判 tool_miss 的依据）")
    sources_count: int
    score_correctness: float | None = None
    score_faithfulness: float | None = None
    score_completeness: float | None = None
    judge_runs: int = Field(description="实际成功打分次数（< runs_per_case 说明有调用失败）")
    passed: bool
    failure_reason: str | None = Field(description="失败归因（通过时为 null）")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failure_reason_label(self) -> str:
        """失败原因的中文标签。

        在 schema 层算而不是落库：**它是展示用的派生值**，存进库里就多一处
        可能与枚举定义漂移的地方（改了标签，历史数据里还是旧文案）。
        """
        from app.services.failure_taxonomy import describe

        return describe(self.failure_reason)


class EvalCaseResultDetailOut(EvalCaseResultOut):
    """明细详情：补上复现/重判所需的原材料。"""

    retrieved_chunks: str = Field(description="本次检索到的片段（faithfulness 的对照物）")
    judge_raw: list | None = Field(
        default=None,
        description="各次打分的原始输出 —— 存它才能事后判断\"某个差异是否超出门槛\"，"
                    "而不是只看到一个被平均掉的分数",
    )
