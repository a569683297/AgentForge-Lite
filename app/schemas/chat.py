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
"""

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    """一条来源：与回答里 [n] 编号一一对应。"""

    index: int = Field(description="引用编号，对应回答文本里的 [n]")
    source: str = Field(description="来源文档标识（文件名/标题）")
    content: str = Field(description="命中的原文片段")
    similarity: float | None = Field(default=None, description="余弦相似度，越大越相似")


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


class ChatRequest(BaseModel):
    """POST /api/chat 的请求体。"""

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="用户本轮的输入文本",
    )
    session_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description=(
            "会话 ID。同一会话的多轮对话必须传同一个值；"
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
        description="本次会话 ID；前端保存后，后续轮次通过请求体回传以延续上下文",
    )
