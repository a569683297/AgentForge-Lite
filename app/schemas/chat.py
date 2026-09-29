"""
对话相关的数据结构（D11）
==========================
D10 之前 run_agent 返回 str（只有回答文本）。
D11 要给回答配一张「来源映射表」，让回答里的 [1] 能定位回原文，
所以出口变成结构化的 ChatResult。

用 pydantic BaseModel 的原因：
1. 字段有类型校验，来源数据缺字段会在构造时报错，而不是悄悄传下去
2. 未来接 HTTP 接口时可以直接作为 response_model，FastAPI 自动序列化
3. 比裸 dict 更能说明「这个接口返回什么」，等于可执行的接口文档

D11 补充（HTTP 层）：新增 ChatRequest / ChatResponse，
把服务层的结果搬上 HTTP 边界。校验交给 pydantic —— 请求体不合法时
FastAPI 直接返回 422，路由函数里一行校验代码都不用写。

D14 变化：session_id 的类型从 str（正则宽松匹配）收口为 uuid.UUID。
为什么必须收口：sessions.id 是 PG 的 uuid 类型，它会把自己看到的值
规范化成带横杠的标准格式；而 D8 起服务端生成的是 32 位无横杠 hex，
两者拼出来的 Redis key 不是同一个字符串 —— 从 /api/sessions 拿到 id
再回来对话，热窗口会读不到（静默退化成走兜底）。
把类型交给 uuid.UUID 还有个附带好处：非法值在 pydantic 层直接 422，
不用自己写正则（旧正则 ^[A-Za-z0-9_-]{1,64}$ 会放行 "abc" 这种非 uuid 值）。
"""

import uuid

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    """一条来源：与回答里 [n] 编号一一对应。"""

    index: int = Field(description="引用编号，对应回答文本里的 [n]")
    source: str = Field(description="来源文档标识（文件名/标题）")
    content: str = Field(description="命中的原文片段")
    similarity: float | None = Field(default=None, description="余弦相似度，越大越相似")
    page_ref: str | None = Field(
        default=None,
        description="页码引用（如 p.3）；仅 PDF 类文档有，其他格式为 null（D12 新增）",
    )


class ChatResult(BaseModel):
    """一次 Agent 调用的完整结果：回答 + 来源映射表。"""

    answer: str = Field(description="LLM 生成的回答（可能含 [1] [2] 形式的引用编号）")
    sources: list[SourceItem] = Field(
        default_factory=list,
        description="本次对话累计检索到的来源，供前端把 [n] 映射回原文",
    )
    invalid_citations: list[int] = Field(
        default_factory=list,
        description=(
            "回答里引用了但来源表中不存在的编号（LLM 幻觉引用）。"
            "D11 只做检测与记录，不强行改写回答——"
            "改写会引入新的不确定性，等 D17 评测体系量化后再定策略。"
        ),
    )
    tool_calls: list[str] = Field(
        default_factory=list,
        description=(
            "本轮实际调用的工具名序列（按调用顺序）。D22 新增。"
            "与 sources 同族的『过程的结构化产物需要一条出口』："
            "评测要判定『C 类题有没有选对工具』（tool_miss），"
            "而这件事**从答案文本里看不出来** —— 答案可能答对了却完全没查库。"
            "这些信息本来就在图内部的 messages 里，只是原先没有出口。"
        ),
    )


class ChatRequest(BaseModel):
    """POST /api/chat 的请求体。"""

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="用户本轮的输入文本",
    )
    session_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "会话 ID（标准 UUID；也接受无横杠的 32 位写法，pydantic 会归一化）。"
            "同一会话的多轮对话必须传同一个值；"
            "不传则由服务端新建并在响应里返回，后续轮次带上即可延续上下文。"
        ),
    )


class ChatResponse(ChatResult):
    """
    POST /api/chat 的响应体：在 ChatResult 上补一个会话 ID。

    为什么用继承而不是把 session_id 直接加进 ChatResult：
    session_id 是「传输层」的概念（HTTP 请求之间靠它串起同一个会话），
    而 ChatResult 是「服务层」的返回值——run_agent 已经通过入参拿到它了，
    不需要在自己的结果里再回带一份。分层别串味。
    """

    session_id: str = Field(
        description=(
            "本次会话 ID（D14 起为标准 UUID 格式：36 位、带横杠）。"
            "前端保存后，后续轮次通过请求体回传即可延续上下文"
        ),
    )
