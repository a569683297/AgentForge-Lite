"""
会话与消息持久化服务（D14）
============================
补 PRD D10 的账：把「会话 + 消息」真正写进 PostgreSQL。

为什么需要它（补的是哪一块）：
D8 起对话记忆只有 Redis 一层（8 轮 + TTL 24h）——超 8 轮、超 24h、
或 Redis 重启，历史就永久丢失且没有任何兜底。PRD F5.3 的设计是
「消息全量 PostgreSQL + 热窗口 Redis」两层，本模块补 PG 这一层。

与 memory_service 的分工（面试高频追问）：
    memory_service（Redis）= 喂给 LLM 的窗口，只留 user + assistant
    本模块（PG）           = 全量留痕，含 role='tool' 的消息
两者不是同一份数据的两个拷贝，而是两条不同的路：
    落库要全量 —— 审计/回溯/评测，以及 Redis 失效后的兜底回填都靠它
    喂 LLM 只留 user+assistant —— function calling 要求 tool_calls 与 tool
    消息成对出现，窗口里只留一半下一轮请求必 400，所以干脆两者都不进窗口

本模块只有六个出口（会话数据只从这里进库，其余模块不许直接写）：
    ensure_session()     会话不存在则创建（PRD F1.3）
    append_messages()    本轮消息全量落库 + 刷新 updated_at（PRD F1.4）
    list_sessions()      会话列表（PRD §11 GET /api/sessions）
    get_session()        单个会话（用于区分 404 与"空会话"）
    get_messages()       某会话历史消息（PRD §11 GET /api/sessions/{id}/messages）
    get_recent_dialogue() 最近 N 轮"对话语义"消息（D15：供 Redis 热窗口回填）

事务约定（承接 D12 的教训）：
    session.add() 只把对象放进内存队列，commit() 才真正写库；
    所以一个「请求级」写入（建会话 + 写消息 + 刷 updated_at）必须在
    同一个 commit 里完成 —— 失败整批回滚，不留半截记录。
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.message import Message
from app.models.session import Session

TITLE_MAX_LEN = 30          # 会话标题取首条用户消息的前 N 字
DEFAULT_TITLE = "新会话"
MESSAGES_PAGE_LIMIT = 500   # 单次最多返回多少条历史消息（防一次拉爆）


def _make_title(title_hint: str | None) -> str:
    """把首条用户消息裁成一个像样的标题。"""
    text = (title_hint or "").strip()
    if not text:
        return DEFAULT_TITLE
    return text[:TITLE_MAX_LEN]


async def ensure_session(session_id: uuid.UUID, *, title_hint: str | None = None) -> bool:
    """
    会话不存在则创建（存在则什么都不改）。

    为什么用 INSERT ... ON CONFLICT DO NOTHING 而不是「先查再插」：
    先查再插在并发下有空窗 —— 两个请求同时发现「不存在」，于是都去插，
    后一个撞主键报错。交给数据库做原子判断，就没有这个空窗。

    Returns:
        True = 本次新建；False = 早已存在（此时 title 保持不变）
    """
    # RETURNING 在这里是「探针」：只有真的插进去了才会返回一行，
    # 被 ON CONFLICT 拦下时返回 None —— 于是我们免费知道了"是否新建"。
    stmt = (
        pg_insert(Session)
        .values(id=session_id, title=_make_title(title_hint))
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(Session.id)
    )
    async with async_session_factory() as db:
        created = (await db.execute(stmt)).scalar() is not None
        await db.commit()          # 不 commit 就等于没写（D12 的教训）
    logger.info("会话已确保存在 session=%s 本次新建=%s", session_id, created)
    return created


async def append_messages(
    session_id: uuid.UUID,
    messages: list[dict],
    *,
    title_hint: str | None = None,
) -> int:
    """
    把本轮产生的消息全量落库，并刷新会话的 updated_at。

    一个事务里做三件事（顺序是有讲究的）：
        ① 建会话（不存在时）—— messages.session_id 有外键指向 sessions.id，
           会话不存在就直接写消息会触发外键报错，所以必须排在最前
        ② 写消息（全量：user / assistant / tool 都进）
        ③ 刷 sessions.updated_at —— 只有这一步做了，"最近会话"排序才正确；
           注意 ORM 的 onupdate 只在「ORM 层 UPDATE 该行」时触发，
           光插消息不会碰 sessions 行，所以这里显式 UPDATE

    Args:
        messages: [{"role": "user"|"assistant"|"tool", "content": str,
                    "tool_calls": list|None}, ...]
                  content 允许为空串（带 tool_calls 的 assistant 消息就没有正文）
    Returns:
        实际写入的消息条数
    """
    if not messages:
        return 0

    rows = [
        Message(
            session_id=session_id,
            role=m.get("role") or "unknown",
            # content 列是 NOT NULL，而 assistant 的"纯工具调用"消息内容为空，
            # 所以这里用空串兜底，而不是让它变成 None 撞约束
            content=(m.get("content") or ""),
            tool_calls=m.get("tool_calls"),
        )
        for m in messages
    ]

    async with async_session_factory() as db:
        try:
            # ① 建会话（幂等，已存在则跳过）
            await db.execute(
                pg_insert(Session)
                .values(id=session_id, title=_make_title(title_hint))
                .on_conflict_do_nothing(index_elements=["id"])
            )
            # ② 消息入队（此时只在内存，还没落库）
            db.add_all(rows)
            # ③ 刷新会话时间戳（让会话列表能按最近活跃排序）
            await db.execute(
                update(Session).where(Session.id == session_id).values(updated_at=func.now())
            )
            await db.commit()      # ← 到这里三条语句才作为一个整体落库
        except Exception:
            await db.rollback()    # 任一步失败 → 整批回滚，不留半截记录
            logger.exception("消息落库失败，已回滚 session=%s", session_id)
            raise

    logger.info("消息已落库 session=%s 条数=%d", session_id, len(rows))
    return len(rows)


async def list_sessions(limit: int = 50, offset: int = 0) -> list[dict]:
    """
    会话列表，按最近活跃倒序（最新在前的）。

    返回 dict 而不是 ORM 对象：Session 实例带着对数据库连接的引用，
    session 关闭后访问它的属性会触发懒加载并报错；在这里一次性转成
    纯数据结构，调用方（API 层）拿到的就是"已死"的安全数据。
    """
    stmt = (
        select(Session)
        .order_by(Session.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    async with async_session_factory() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [
        {"id": str(s.id), "title": s.title, "updated_at": s.updated_at}
        for s in rows
    ]


async def get_session(session_id: uuid.UUID) -> dict | None:
    """
    按 id 取单个会话；不存在返回 None（供 API 层判 404）。

    为什么要专门有它：GET /api/sessions/{id}/messages 得区分两种情况 ——
    「会话不存在」（应该 404）与「会话存在但还没有消息」（应该 200 + 空数组）。
    只查 messages 表的话，两者都表现为空数组，前端根本分不出来。
    """
    async with async_session_factory() as db:
        row = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar_one_or_none()
    if row is None:
        return None
    return {"id": str(row.id), "title": row.title, "updated_at": row.updated_at}


async def get_messages(
    session_id: uuid.UUID,
    limit: int = MESSAGES_PAGE_LIMIT,
) -> list[dict]:
    """
    取某会话的历史消息（按时间正序）。

    排序为什么带 id：created_at 是数据库 now()，同一事务里写入的多条消息
    时间戳可能完全相同，只按它排序结果不稳定；id 是自增主键，天然有序，
    拿它做第二排序键才能保证「问在前、答在后」。
    """
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(limit)
    )
    async with async_session_factory() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "role": m.role,
            "content": m.content,
            "tool_calls": m.tool_calls,
            "created_at": m.created_at,
        }
        for m in rows
    ]


async def get_recent_dialogue(
    session_id: uuid.UUID,
    turns: int = 8,
) -> list[dict]:
    """
    取最近 N 轮「对话语义」消息（时间正序），供 Redis 热窗口回填使用（D15）。

    与 get_messages 的区别（别混用）：
        get_messages()         → 给 API 用：**全量**、含 tool 行、含 created_at
        get_recent_dialogue()  → 给记忆层用：**最近 N 轮**、只留对话语义、结构精简

    过滤条件的两个依据（缺一条都会出事）：
      ① role IN ('user','assistant')
         排除 role='tool' 的行。
      ② tool_calls IS NULL
         排除「纯决策」的那条 assistant（它带 tool_calls、content 常为空）。
         为什么必须连它一起排掉：function calling 协议要求 tool_calls 与 tool
         消息**成对**出现，只留下其中一半，下一轮请求必 400。
         ⚠ 这个条件能生效的前提是「None 真的写成了 SQL NULL」——
           D14 缺陷 ③ 之前 JSONB 列存的是 JSON 字面量 null，`IS NULL` 恒为假，
           本查询会静默返回 0 行（回填变成空回填，页面却一切正常）。

    为什么倒序取再反转：要的是「最近」N 轮，SQL 只能按 id 倒序 + LIMIT 拿到；
    但喂给 LLM 必须是时间正序，所以在 Python 侧反转回来。
    一轮固定产出 2 条干净消息（user + 最终 assistant），所以 limit = turns × 2。
    """
    limit = max(1, turns) * 2
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.role.in_(("user", "assistant")),
            Message.tool_calls.is_(None),
        )
        .order_by(Message.id.desc())
        .limit(limit)
    )
    async with async_session_factory() as db:
        rows = (await db.execute(stmt)).scalars().all()

    # reversed：把「倒序取出的最近 N 条」还原成时间正序
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]
