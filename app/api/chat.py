"""
对话接口（D11 补充；M6 优化② 增补流式出口）
=============================================
POST /api/chat         —— 非流式对话接口（D11 起）。
POST /api/chat/stream  —— SSE 流式对话接口（M6 优化②）。

两条出口的关系：
服务层是同一个（run_agent / run_agent_stream，见 agent_service），差别只在
"回答整段返回"还是"边跑边发"。非流式出口保留，因为它是**不依赖流式基础设施**
的稳定通道：curl 冒烟、脚本、以及"SSE 链路（含 nginx 缓冲配置）出问题时"
回退对比的基准 —— 两条链路各自独立可测。

一个早年写下的错误判断（如实保留教训）：D11 时这里写过"将来上流式时，
前端本来就要把 fetch 改写成 EventSource"。**这条判断是错的** ——
EventSource 只能发 GET、带不了请求体，而对话是 POST + {message, session_id}。
后来实际用的是 fetch + ReadableStream 手写帧解析（协议层仍是标准 SSE）。

端点约定：
    POST /api/chat           请求 {"message": "...", "session_id": "可选"}
                             响应 {"session_id", "answer", "sources", "invalid_citations", "tool_calls"}
    POST /api/chat/stream    请求体同上；
                             响应 text/event-stream，帧协议见 chat_stream 的 docstring

D36 修复：非流式这一层此前**漏传 `tool_calls`**（`ChatResult` 有、`ChatResponse` 有、
`default_factory=list` 一兜 → 响应里恒为 `[]`，**不报错、不崩溃**）。
后果：前端实时对话的「工具卡片」永远不显示（刷新后从 PG 重读才出现）、
演示脚本读不到任何工具调用。**教训**：给返回模型加字段时，这一层必须同步传，
否则默认值会把它悄悄吃掉（铁律 10「改了返回类型要改所有调用点」）。

D14 变化：session_id 在进程内是 uuid.UUID 对象（请求侧由 pydantic 校验并转换），
只在响应边界转成字符串。会话的持久化（落库）由服务层负责，这一层仍然不碰数据库。

职责边界（这一层只做三件事）：
    1. 补全 session_id（不传就新建）
    2. 调服务层，把业务逻辑全部留在服务层（流式出口只额外做"事件 → SSE 帧"的编码）
    3. 记两条日志（请求进 / 响应出），方便对着日志排查「哪一轮调了工具」

刻意不做的事：不在这里写任何业务判断（检索、组装 prompt、校验引用都在服务层），
也不做参数校验（交给 pydantic + FastAPI，非法请求直接 422）。
"""

import json
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.logging import logger
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.agent_service import run_agent, run_agent_stream

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
    # D14：不再用 uuid4().hex —— 会话内一律用 uuid.UUID 对象，
    # 只在响应边界转成字符串，保证「一个会话 = 一个 Redis key = 一行 sessions 记录」。
    session_id = req.session_id or uuid.uuid4()

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
        session_id=str(session_id),
        answer=result.answer,
        sources=result.sources,
        invalid_citations=result.invalid_citations,
        # ⚠ 这一行不能省：ChatResult.tool_calls 有默认值 []，漏传不会报错，
        #    只会让前端「工具卡片」静默拿不到数据（D36 踩过）。
        #    回归守卫：tests/integration/test_api_endpoints.py::
        #    test_chat_forwards_every_chatresult_field_to_http（哨兵值法）。
        tool_calls=result.tool_calls,
    )


# ============================================================
# 流式对话（M6 优化②）：SSE 出口
# ============================================================
def _sse_frame(event: dict) -> str:
    """
    把一个事件编成一条 SSE 帧（`data: {json}\\n\\n`）。

    协议取舍（前端解析器按这个写）：
      - 只用 `data:` 字段，**不用 SSE 的 `event:` 字段**：事件类型写在 JSON 的
        `type` 里 —— 一帧一个 object，前端 parse 一次到位，少解析一层文本协议；
      - `ensure_ascii=False`：中文原样进帧（帧更小；前端 TextDecoder 按 UTF-8 解）；
      - 末尾固定的 `\\n\\n` 是 SSE 的**帧分隔符**（不是格式化空行）——
        前端就按它切帧，缺了会把两帧粘成一条。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat/stream", summary="与 Agent 对话（SSE 流式）")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """
    流式版对话接口：与 /api/chat 同一条服务链路，差别只在"边跑边发"。

    帧协议（事件形状由服务层 run_agent_stream 定义，本层只做编码——见其 docstring）：
        data: {"type": "meta",  "session_id": "..."}      流已建立（第一帧）
        data: {"type": "token", "text": "..."}            答案增量
        data: {"type": "reset"}                           清掉当前气泡的中间文本
        data: {"type": "tool",  "name": ..., "phase": "start"}
        data: {"type": "tool",  "name": ..., "phase": "end", "ok": true|false}
        data: {"type": "done",  "session_id": ..., "answer": ..., "sources": ...,
                                "invalid_citations": ..., "tool_calls": ...}
        data: {"type": "error", "message": "..."}         链路异常

    ⚠ 两个必须知情的事实：
      ① **HTTP 状态码在开流时就已经是 200** —— 之后无论发生什么（LLM 挂掉、
         持久化失败），错误都只能以 error 帧的形式传递（SSE 的固有限制：
         响应头先于内容发出）。前端必须监听 error 帧，不能只看状态码。
      ② 为什么不是 EventSource（面试常问）：EventSource 只能发 GET、带不了
         请求体，而对话是 POST + {message, session_id}；所以前端用
         fetch + ReadableStream 手写帧解析——协议层仍是标准 SSE。
    """
    session_id = req.session_id or uuid.uuid4()
    logger.info("流式对话请求 session=%s 输入长度=%d", session_id, len(req.message))

    async def event_stream():
        # meta 必须最先发：前端据此确认"流已建立"；日志/测试也靠它立刻关联会话
        yield _sse_frame({"type": "meta", "session_id": str(session_id)})
        try:
            async for event in run_agent_stream(session_id, req.message):
                yield _sse_frame(event)
        except Exception as e:
            # 图跑挂 / 写回失败都在这里兜住：**不能**让它表现为"连接无声断掉"
            # （前端只看到流结束、不知道原因）—— 显式发一帧 error，前端有据可依。
            # 详细堆栈进日志（服务端排查用），帧里只带一句可读信息（用户可见）。
            logger.exception("流式对话失败 session=%s", session_id)
            yield _sse_frame({"type": "error", "message": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # SSE 三件套：不缓存；并主动告诉反向代理"这一条不要缓冲"。
            # X-Accel-Buffering 是 nginx 的约定（响应头层防御；nginx 配置层
            # 也关了 proxy_buffering，两层同时到位——漏层会让流式"看起来没生效"）
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
