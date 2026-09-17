"""
对话接口（D11 补充）
====================
POST /api/chat —— 非流式对话接口。

为什么先做非流式：
PRD 规划的是 SSE 流式（逐字返回），但那要求前端用 EventSource 接收、
还要处理断线重连，是一块独立的工作量。项目现在还没有前端，先用非流式
JSON 把「Agent 服务层 → HTTP 出口」这段接通——好处是能用 curl 直接看到
完整的 {answer, sources} 结构，确认 D11 的引用定位在真实 HTTP 边界上是否完整。

将来上流式时，前端本来就要把 fetch 改写成 EventSource，所以现在用 JSON
不构成额外的技术债。

端点约定：
    请求  {"message": "问题", "session_id": "可选"}
    响应  {"session_id": "...", "answer": "...", "sources": [...], "invalid_citations": [...]}

职责边界（这一层只做三件事）：
    1. 补全 session_id（不传就新建）
    2. 调 run_agent，把业务逻辑全部留在服务层
    3. 记两条日志（请求进 / 响应出），方便对着日志排查「哪一轮调了工具」

刻意不做的事：不在这里写任何业务判断（检索、组装 prompt、校验引用都在服务层），
也不做参数校验（交给 pydantic + FastAPI，非法请求直接 422）。
"""

import uuid

from fastapi import APIRouter

from app.core.logging import logger
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.agent_service import run_agent

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse, summary="与 Agent 对话（非流式）")
async def chat(req: ChatRequest) -> ChatResponse:
    """
    跑一次完整的 Agent 循环：读记忆 → ReAct（必要时检索知识库）→ 写记忆 → 返回。

    响应里的 sources 是「编号 → 原文」的映射表，与 answer 里的 [n] 一一对应；
    invalid_citations 列出回答里引用了但来源表中不存在的编号（前端可据此降级显示）。
    """
    # 不传 session_id → 服务端新建。这样 curl 裸测一个请求也能跑通；
    # 前端要延续上下文时，把响应里的 session_id 存下来、下一轮回传即可。
    session_id = req.session_id or uuid.uuid4().hex

    logger.info("对话请求 session=%s 输入长度=%d", session_id, len(req.message))

    result = await run_agent(session_id, req.message)

    logger.info(
        "对话响应 session=%s 回答长度=%d 来源=%d 越界引用=%s",
        session_id,
        len(result.answer),
        len(result.sources),
        result.invalid_citations or "无",
    )

    return ChatResponse(
        session_id=session_id,
        answer=result.answer,
        sources=result.sources,
        invalid_citations=result.invalid_citations,
    )
