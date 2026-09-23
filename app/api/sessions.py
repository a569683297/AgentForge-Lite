"""
会话查询接口（D14）
====================
补 PRD §11 的两个只读端点：

    GET /api/sessions                → 会话列表（按最近活跃倒序）
    GET /api/sessions/{id}/messages  → 某会话的完整历史（时间正序）

为什么 D14 之前没有这两个接口：会话数据压根没落库 —— 没有表可查，
接口写出来也只能返回空数组，属于"假接口"。先补数据层，再补接口，顺序不能反。

职责边界：路由层只做「参数校验 + 调服务层 + 组装响应」，一行 SQL 都不写。
SQL 全在 app/services/session_service.py，那里才是会话数据的唯一出口。
"""

import uuid

from fastapi import APIRouter, HTTPException, Query

from app.schemas.session import MessageItem, SessionItem
from app.services.session_service import get_messages, get_session, list_sessions

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=list[SessionItem], summary="会话列表")
async def list_sessions_api(
    limit: int = Query(50, ge=1, le=200, description="最多返回多少条"),
    offset: int = Query(0, ge=0, description="跳过前多少条（分页用）"),
) -> list[SessionItem]:
    """
    按最后活跃时间倒序返回会话列表。

    为什么是倒序：这个接口的用途是「我最近聊过什么」，最新的必须在最上面。
    updated_at 由 append_messages 在每轮对话后刷新，所以它等于"最后说话时间"——
    注意它不会因为"只是查看"而变化，只有真的写入了消息才会动。
    """
    rows = await list_sessions(limit=limit, offset=offset)
    return [SessionItem(**row) for row in rows]


@router.get(
    "/sessions/{session_id}/messages",
    response_model=list[MessageItem],
    summary="会话历史消息",
)
async def get_session_messages_api(
    session_id: uuid.UUID,
    limit: int = Query(500, ge=1, le=2000, description="最多返回多少条"),
) -> list[MessageItem]:
    """
    返回该会话的全部消息（时间正序），**含 role='tool' 的中间消息**。

    为什么保留 tool 消息不隐藏：这是「全量留痕」那份数据的用途本身 ——
    排查"这一轮到底调了什么工具、参数是什么、返回了什么"时，
    去掉 tool 行就什么都看不出来了。前端要展示纯净对话，可以按 role 自己过滤。

    会话不存在时返回 404 而不是空数组：空数组的含义是"会话在，但没说过话"，
    与"会话不存在"是两件完全不同的事，糊在一起前端没法处理。
    """
    session = await get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = await get_messages(session_id, limit=limit)
    return [MessageItem(**row) for row in rows]
