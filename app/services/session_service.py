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

本模块只有十个出口（会话数据只从这里进库，其余模块不许直接写）：
    ensure_session()     会话不存在则创建（PRD F1.3）
    append_messages()    本轮消息全量落库 + 刷新 updated_at（PRD F1.4）
    list_sessions()      会话列表（PRD §11 GET /api/sessions）
    get_session()        单个会话（用于区分 404 与"空会话"）
    get_messages()       某会话历史消息（PRD §11 GET /api/sessions/{id}/messages）
    get_recent_dialogue() 最近 N 轮"对话语义"消息（D15-1：供 Redis 热窗口回填）
    get_summary()         读长期摘要（D15-2：PRD §9.4 注入 system）
    save_summary()        写长期摘要（D15-2：PRD F5.2）
    get_dialogue_stats()  干净对话消息总数 + 热窗口下界（D15-2：摘要触发判据）
    get_dialogue_range()  取一段干净对话消息（D15-2：摘要的输入区间）

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
from app.core.tenant import current_owner, owner_filter
from app.models.message import Message
from app.models.session import Session

TITLE_MAX_LEN = 30          # 会话标题取首条用户消息的前 N 字
DEFAULT_TITLE = "新会话"
MESSAGES_PAGE_LIMIT = 500   # 单次最多返回多少条历史消息（防一次拉爆）


class SessionOwnershipError(Exception):
    """
    会话存在，但不属于调用者。

    为什么用**异常**而不是返回 None：
      `ensure_session` 的语义是"保证这个会话可用"。当它不可用（别人的会话）时，
      继续往下跑就意味着**把消息写进别人的会话**。返回 False 只会被当成
      "已存在"，调用方看不出区别 —— 那是静默越权。抛出来，调用方必须处理。

    ⚠ 接口层要把它翻译成 **404**，不是 403：
      403 等于告诉对方"这个 id 确实存在，只是不给你" —— 这本身是一条信息
      （可以拿来枚举别人有哪些会话）。404 让"不存在"和"不属于你"无法区分。
    """


def _make_title(title_hint: str | None) -> str:
    """把首条用户消息裁成一个像样的标题。"""
    text = (title_hint or "").strip()
    if not text:
        return DEFAULT_TITLE
    return text[:TITLE_MAX_LEN]


async def ensure_session(
    session_id: uuid.UUID,
    *,
    title_hint: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> bool:
    """
    会话不存在则创建（存在则校验归属，不改任何字段）。

    为什么用 INSERT ... ON CONFLICT DO NOTHING 而不是「先查再插」：
    先查再插在并发下有空窗 —— 两个请求同时发现「不存在」，于是都去插，
    后一个撞主键报错。交给数据库做原子判断，就没有这个空窗。

    多租户（2026-10-21）：`ON CONFLICT DO NOTHING` 有两个来源 ——
      「我新建的」和「别人早就建好的」。两者返回的都是 None/无行，
      所以冲突之后必须**再查一次归属**，否则"别人的会话"会被当成"我的会话"
      直接复用（那是最危险的一种越权：写）。多这一次 SELECT 是必要的。

    Args:
        owner_id: 归属用户。None = 系统内部调用（不校验）。
    Returns:
        True = 本次新建；False = 早已存在（此时 title 保持不变）
    Raises:
        SessionOwnershipError: 会话已存在且属于别人（或属于"无主"历史数据）
    """
    # RETURNING 在这里是「探针」：只有真的插进去了才会返回一行，
    # 被 ON CONFLICT 拦下时返回 None —— 于是我们免费知道了"是否新建"。
    stmt = (
        pg_insert(Session)
        .values(id=session_id, title=_make_title(title_hint), owner_id=owner_id)
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(Session.id)
    )
    async with async_session_factory() as db:
        created = (await db.execute(stmt)).scalar() is not None
        await db.commit()          # 不 commit 就等于没写（D12 的教训）

        if not created and owner_id is not None:
            existing_owner = (
                await db.execute(
                    select(Session.owner_id).where(Session.id == session_id)
                )
            ).scalar_one_or_none()
            # 注意：`existing_owner is None` 也算不通过 —— 无主会话（迁移前遗留）
            # 不该被任何人"顺手接管"。这是 fail-closed 的选择。
            if existing_owner != owner_id:
                logger.warning(
                    "拒绝访问他人会话 session=%s 请求者=%s 归属=%s",
                    session_id, owner_id, existing_owner,
                )
                raise SessionOwnershipError("会话不存在")

    logger.info("会话已确保存在 session=%s 本次新建=%s", session_id, created)
    return created


async def append_messages(
    session_id: uuid.UUID,
    messages: list[dict],
    *,
    title_hint: str | None = None,
    trace_id: str | None = None,
    owner_id: uuid.UUID | None = None,
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
        trace_id: 本轮运行的 Langfuse trace id。**只写到 user 消息上** ——
                  一次提问 = 一条 trace = 一条 user 消息；assistant/tool 是同一轮的
                  后续消息，重复挂会让 "按 trace_id 查提问" 一次查出三四行，
                  上层还得再按 role 去重一次（见 Message.trace_id 的注释）。
                  评测模式（persist=False）不会调到这里，所以评测 trace 永远没有提问原文。
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
            # 只有 user 行带 trace_id（见上）
            trace_id=trace_id if (m.get("role") == "user") else None,
        )
        for m in messages
    ]

    async with async_session_factory() as db:
        try:
            # ① 建会话（幂等，已存在则跳过）。owner_id 只对"新建"这一支有意义 ——
            #    已存在的会话归属不会被改写（ON CONFLICT DO NOTHING 的语义），
            #    所以谁想往别人的会话里塞消息，必须先过 ensure_session 的归属校验。
            await db.execute(
                pg_insert(Session)
                .values(id=session_id, title=_make_title(title_hint), owner_id=owner_id)
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


async def list_sessions(
    limit: int = 50, offset: int = 0, *, owner_id: uuid.UUID | None = None
) -> list[dict]:
    """
    会话列表，按最近活跃倒序（最新在前的）。

    返回 dict 而不是 ORM 对象：Session 实例带着对数据库连接的引用，
    session 关闭后访问它的属性会触发懒加载并报错；在这里一次性转成
    纯数据结构，调用方（API 层）拿到的就是"已死"的安全数据。

    `owner_id` 为 None 时取请求上下文里的当前用户（见 core/tenant.py）：
    接口层不需要显式传 —— 但**必须**有上下文，否则就是"不过滤"（系统视角）。
    """
    scoped = owner_id if owner_id is not None else current_owner()
    stmt = (
        select(Session)
        .where(owner_filter(Session.owner_id, scoped))
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


async def get_session(
    session_id: uuid.UUID, *, owner_id: uuid.UUID | None = None
) -> dict | None:
    """
    按 id 取单个会话；不存在（**或不属于调用者**）返回 None（供 API 层判 404）。

    为什么要专门有它：GET /api/sessions/{id}/messages 得区分两种情况 ——
    「会话不存在」（应该 404）与「会话存在但还没有消息」（应该 200 + 空数组）。
    只查 messages 表的话，两者都表现为空数组，前端根本分不出来。

    归属不匹配时与"不存在"返回同一个值，是刻意的：调用方拿不到任何
    "这个 id 存在但你没权限"的信号（见 SessionOwnershipError 的说明）。
    """
    scoped = owner_id if owner_id is not None else current_owner()
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(Session)
                .where(Session.id == session_id)
                .where(owner_filter(Session.owner_id, scoped))
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return {"id": str(row.id), "title": row.title, "updated_at": row.updated_at}


async def get_messages(
    session_id: uuid.UUID,
    limit: int = MESSAGES_PAGE_LIMIT,
    *,
    owner_id: uuid.UUID | None = None,
) -> list[dict]:
    """
    取某会话的历史消息（按时间正序）。

    排序为什么带 id：created_at 是数据库 now()，同一事务里写入的多条消息
    时间戳可能完全相同，只按它排序结果不稳定；id 是自增主键，天然有序，
    拿它做第二排序键才能保证「问在前、答在后」。

    多租户：用 JOIN sessions 做归属过滤，而不是先查一次会话再查消息 ——
    同样是一次往返，但**没有"先查后查之间会话被转手"的空窗**，
    而且不需要调用方记得先校验（漏一次就泄露整段对话）。
    """
    scoped = owner_id if owner_id is not None else current_owner()
    stmt = (
        select(Message)
        .join(Session, Session.id == Message.session_id)
        .where(Message.session_id == session_id)
        .where(owner_filter(Session.owner_id, scoped))
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
        .where(*_clean_dialogue_filter(session_id))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    async with async_session_factory() as db:
        rows = (await db.execute(stmt)).scalars().all()

    # reversed：把「倒序取出的最近 N 条」还原成时间正序
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


# ============================================================
# D15 第二段：摘要压缩需要的四个出口
# ============================================================
CLEAN_ROLES = ("user", "assistant")


def _clean_dialogue_filter(session_id: uuid.UUID) -> tuple:
    """
    「对话语义」的过滤条件 —— 所有按"轮"来数的查询都从这取，规则只写一遍。

    两个条件必须**成对**使用：
      ① role IN ('user','assistant')  → 排掉 role='tool' 的行
      ② tool_calls IS NULL            → 排掉 plan 的"纯决策" assistant 行
    它们是配对产生的一对（带 tool_calls 的 assistant ↔ 它触发的 tool 消息）。
    只留其中一半，下一轮请求必 400 —— 所以要么都留、要么都排，不能只排一半。

    历史教训（为什么这里要写一行警告）：②依赖「None 真的写成 SQL NULL」。
    D14 缺陷 ③ 之前 JSONB 列存的是 JSON 字面量 null，`IS NULL` 恒为假 ——
    这些查询会**静默返回 0 行**，页面一切正常，只是"历史不见了"。
    """
    return (
        Message.session_id == session_id,
        Message.role.in_(CLEAN_ROLES),
        Message.tool_calls.is_(None),
    )


async def get_summary(session_id: uuid.UUID) -> str | None:
    """
    读会话的长期摘要（PRD §9.4：组装上下文时作为 system 前缀）。

    找不到会话、或会话还没摘过 → 返回 None（调用方据此决定"拼不拼这一段"）。
    """
    async with async_session_factory() as db:
        value = (
            await db.execute(select(Session.summary).where(Session.id == session_id))
        ).scalar_one_or_none()
    return value or None


async def save_summary(session_id: uuid.UUID, summary: str) -> None:
    """
    写长期摘要（PRD F5.2）。

    两个刻意的选择：
      ① **空摘要直接拒绝**：宁可留着上一版摘要，也不要用一个空字符串把已有记忆抹掉。
         触发场景是真实的 —— LLM 偶发返回空串/只有空白。
      ② ⚠ 这个 UPDATE 会**连带刷新 updated_at**：ORM 的 onupdate 对任何针对该表的
         UPDATE 语句生效，我们只想改 summary，却动了时间戳。
         实际影响可以忽略（它就发生在同一请求内、紧跟 append_messages 的那次刷新），
         但写在这里是因为它反直觉 —— 别以为"没写 updated_at 就没动它"。
    """
    if not summary.strip():
        return
    async with async_session_factory() as db:
        await db.execute(
            update(Session).where(Session.id == session_id).values(summary=summary)
        )
        await db.commit()
    logger.info("长期摘要已更新 session=%s 长度=%d", session_id, len(summary))


async def get_dialogue_stats(session_id: uuid.UUID, *, window_size: int) -> dict:
    """
    摘要的触发判据：干净对话消息总数 + 热窗口下界 id（D15-2）。

    Returns:
        {
          "total": 干净对话消息条数（user + 最终 assistant，不含 tool 与决策行）,
          "window_lower_id": 最近 window_size 条里最早那条的 id；
                             条数不足 window_size 时为 None（= 还没有消息滑出窗口）
        }

    为什么"轮数"必须从 PG 数，不能从 Redis 窗口数（面试高频）：
        Redis 窗口最多 16 条（8 轮），**永远数不出 20 轮** —— 用它当判据，
        摘要永远不会触发。而且 Redis 有 TTL、会重启，只有 PG 是权威源（F5.3）。

    为什么两个查询放同一个数据库会话里：它们是同一时刻的一致视图。
    分开查的话，两次查询之间若有并发写入，算出的边界会互相矛盾。
    """
    conds = _clean_dialogue_filter(session_id)
    async with async_session_factory() as db:
        total = (
            await db.execute(select(func.count()).select_from(Message).where(*conds))
        ).scalar_one()
        ids = (
            (
                await db.execute(
                    select(Message.id)
                    .where(*conds)
                    .order_by(Message.id.desc())
                    .limit(max(1, window_size))
                )
            )
            .scalars()
            .all()
        )

    # ids 是倒序的，所以最后一个是"最近 window_size 条里最早的一条"= 窗口下界。
    # total <= window_size 说明没有任何消息滑出窗口，此时没有可摘要的区间 → None。
    lower = ids[-1] if (total > window_size and ids) else None
    return {"total": total, "window_lower_id": lower}


async def get_dialogue_range(
    session_id: uuid.UUID,
    *,
    after_id: int | None = None,
    before_id: int | None = None,
    limit: int = 40,
) -> list[dict]:
    """
    取一段干净对话消息（时间正序，含 id），作为摘要压缩的输入（D15-2）。

    区间是**左开右开** `(after_id, before_id)`：
        after_id  = 上次摘要的游标（None = 从最早开始）
        before_id = 热窗口下界（None = 一直到最新）

    右开的原因：窗口里的消息还在 Redis 活着、每轮都完整喂给 LLM，
    再摘要一遍就是重复烧 token。这正是「摘要只覆盖已滑出窗口那段」的由来。

    返回带 id 是关键：调用方要用**本批最后一条的 id** 推进游标，
    记录"下次从哪继续"。少了它，增量摘要就退化成每轮全量重摘。
    """
    conds = list(_clean_dialogue_filter(session_id))
    if after_id is not None:
        conds.append(Message.id > after_id)
    if before_id is not None:
        # 严格小于：窗口下界那条消息本身还在 Redis 窗口里，不归摘要管
        conds.append(Message.id < before_id)

    stmt = (
        select(Message)
        .where(*conds)
        .order_by(Message.id.asc())
        .limit(max(1, limit))
    )
    async with async_session_factory() as db:
        rows = (await db.execute(stmt)).scalars().all()

    return [{"id": m.id, "role": m.role, "content": m.content} for m in rows]
