"""
评测接口的数据结构（D21）
==========================
按 PRD §11 的定义，`GET /api/eval/cases` 的响应是 `[{id, category, question}]` 这个形状 ——
**不含参考答案**。这里把形状略微扩展（补上 case_key / difficulty / is_negative），
但**依然不含 reference / evidence**，理由：

    这两列是"考卷的答案页"。列表接口是最容易被顺手接进前端页面的地方，
    参考答案一旦在列表里返回，就很容易被展示出来 —— 而调试时人看到答案、
    或被测系统在某个环节读到答案，评测就**不再是盲测**了。
    要看答案请走单条详情接口（GET /api/eval/cases/{case_key}），那是一个**显式动作**。

这不是防攻击（内部资料，本来就不对外），是防手滑。
"""

from pydantic import BaseModel, ConfigDict, Field


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
