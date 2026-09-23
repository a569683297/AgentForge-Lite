"""
对话记忆服务（两层：Redis 热窗口 + PostgreSQL 兜底）
=====================================================
给 Agent 加上"短期记忆"：记住最近几轮对话，让多轮对话有上下文。

设计要点（为什么这么做）：
1. **滑窗**：只存最近 N 轮，新的进来、旧的挤出去（防止 token 无限膨胀）
2. **Redis**：对话窗口是热数据（高频读写）→ Redis 快；TTL 自动过期，不用手动清理
3. **Upsert 不 Append**：读出整个窗口 → 合并新消息 → 裁剪 → 整体写回
   为什么不能用 rpush/append？
   - 重试/重放会写入重复消息
   - 无法裁剪（append 是单向的，删不掉最旧的）
4. **只存 user 提问 + 最终回答（纯文本）**：不存中间的 tool_calls / tool 消息
   为什么？因为 function calling 协议要求 tool_calls 必须与 tool 消息成对出现；
   如果只存一半（比如存了 tool_calls 却丢了 tool 结果），下一轮请求会 400。
   所以记忆层只保留"对话语义"，工具调用细节留给单轮内处理。
5. **PG 兜底（D15 新增）**：Redis 未命中时从 PostgreSQL 取最近 N 轮并**回填**。
   补的是 PRD F5.3 的后半句 —— 「消息全量 PostgreSQL，热窗口 Redis」。
   D14 之前只有「写 PG」没有「读 PG」，Redis 一过期或一重启，历史就永久消失。
6. **摘要压缩（D15 第二段）**：会话累计超过 20 轮后，把**已滑出窗口**的那一段
   交给 LLM 压成摘要，存进 `sessions.summary`（PRD F5.1 后半 / F5.2 / §9.4）。
   覆盖区间 = `[游标, 窗口下界)` —— 左边是已摘过的、右边是还在窗口里的，
   两头都不该重摘，中间那段才是唯一该摘的（详见 maybe_summarize）。
7. **容错（D15 第二段）**：Redis 是**可以坏的一层**。所有 Redis 操作都过
   `_redis_get/_redis_set/_redis_delete` 三个 helper，失败只记日志不抛 ——
   读失败当场走 PG 兜底；写失败本轮窗口不更新，靠 PG 全量兜底补齐。
   ⚠ 诚实说明自愈的边界：写失败时若 Redis 里**还有旧窗口**（命中），
   下一轮不会触发兜底，缺的那一轮要等 TTL（24h）到期才补齐 ——
   是**延迟自愈**，不是"立刻自愈"，也不是"永久丢失"（PG 里有全量）。

Redis 数据结构（字符串存 JSON 数组）：
  key:   session:{session_id}:window         最近 8 轮对话（本模块的主体）
  key:   session:{session_id}:summary_upto   摘要游标：上次摘到哪条消息 id
  value: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
  TTL:   窗口 86400 秒（24 小时）／游标 7 天（见 SUMMARY_CURSOR_TTL 说明）
"""

import json
import uuid

from app.core.logging import logger
from app.core.redis import redis_client

# ---- 滑窗配置 ----
WINDOW_TURNS = 8                          # 保留最近 8 轮对话
MAX_MESSAGES = WINDOW_TURNS * 2           # 每轮 = user + assistant = 2 条消息
WINDOW_TTL = 86400                        # 24 小时过期（D4 学的 TTL）

# ---- 摘要压缩配置（D15 第二段）----
SUMMARY_TRIGGER_TURNS = 20     # PRD F5.2：累计超过 20 轮才启动摘要
SUMMARY_MIN_MESSAGES = 4       # 攒够 2 轮（4 条）才摘一次 —— 见 maybe_summarize 的取舍说明
SUMMARY_BATCH_MESSAGES = 40    # 单批最多并入多少条（20 轮），防止一次请求无界工作
SUMMARY_TEMPERATURE = 0.1      # PRD §8.4 场景参数：摘要用 0.1（要"忠实压缩"，不要创作）
SUMMARY_CURSOR_TTL = 7 * 86400 # 游标 7 天：比窗口 TTL 长（窗口过期重建后仍能续摘），
                               # 但仍有 TTL —— 否则每个会话永久留一个 key。丢了只会导致
                               # "重摘一次"（结果仍正确，多花一次 LLM 调用）。


# ============================================================
# 容错层：所有 Redis 操作都从这里走
# ============================================================
# 为什么需要它（D15 第二段）：修复前 redis_client.get 是裸调用 —— Redis 服务一挂，
# /api/chat 直接 500，连 PG 兜底都走不到。所谓"兜底"只在"Redis 里没有这个 key"
# 时生效，不在"Redis 不可用"时生效，这等于没有兜底。
#
# 三个 helper 的共同约定：失败 = 记日志 + 返回"什么都没拿到"，绝不向上抛。
#
# 日志为什么带 err=%s 但不带 exc_info=True：
# "降级"是**可预期的高频事件**（Redis 一挂，每个请求的每次操作都会走这里），
# 带堆栈意味着一次故障刷出几十行 traceback，把真正的错误埋掉。
# 异常类型 + 消息（如 ConnectionError: ...）已经足够定位问题；真要看堆栈，
# 把那行 warning 单独调成 exception 即可。
async def _redis_get(key: str) -> str | None:
    """读 Redis；失败返回 None（调用方据此走兜底/降级）。"""
    try:
        return await redis_client.get(key)
    except Exception as e:
        # 用 warning 而不是 error：这不是要人半夜爬起来的问题，
        # 而是一条"已降级"的线索 —— 对话仍然能继续。
        logger.warning("Redis 读取失败，已降级 key=%s err=%s", key, e)
        return None


async def _redis_set(key: str, value: str, *, ex: int) -> bool:
    """
    写 Redis；失败返回 False。

    ⚠ 写失败的自愈边界（别记错）：本轮窗口不会被更新。
      · 若 key 已不存在 → 下一轮读未命中 → PG 兜底重建 → **下一轮即自愈**
      · 若 key 还在（有旧窗口）→ 下一轮**命中旧窗口**，兜底不会触发 →
        缺的那一轮要等 TTL 到期才补齐 → **延迟自愈**
    两种情况下历史都没丢（PG 存全量），区别只是"什么时候补齐"。
    """
    try:
        await redis_client.set(key, value, ex=ex)
        return True
    except Exception as e:
        logger.warning("Redis 写入失败，本轮记忆未更新 key=%s err=%s", key, e)
        return False


async def _redis_delete(key: str) -> bool:
    """删 Redis key；失败返回 False（删不掉只会留下过期数据，不会丢数据）。"""
    try:
        await redis_client.delete(key)
        return True
    except Exception as e:
        logger.warning("Redis 删除失败 key=%s err=%s", key, e)
        return False


def _summary_cursor_key(session_id: uuid.UUID | str) -> str:
    """
    摘要游标的 key：记录"上次摘到哪条消息 id"。

    为什么敢放 Redis（而不是像摘要一样放 PG）：
    它是**可丢失的**缓存 —— 丢了只会导致"下次重新摘一遍"（结果仍然正确，
    只是多花一次 LLM 调用），属于"降级只损失效率、不损失正确性"。
    这是判断"一份数据该不该放缓存"的标准：先问丢了会怎样。
    """
    return f"session:{session_id}:summary_upto"


async def _get_cursor(session_id: uuid.UUID | str) -> int | None:
    """读摘要游标；没有/读不到/值损坏都返回 None（None = 从头摘）。"""
    raw = await _redis_get(_summary_cursor_key(session_id))
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("摘要游标值异常，按无游标处理 session=%s", session_id)
        return None


async def _set_cursor(session_id: uuid.UUID | str, message_id: int) -> bool:
    """推进摘要游标；失败返回 False（下次会重摘这一段 —— 正确性不受影响）。"""
    return await _redis_set(
        _summary_cursor_key(session_id), str(message_id), ex=SUMMARY_CURSOR_TTL
    )


def _window_key(session_id: uuid.UUID | str) -> str:
    """
    生成 Redis key。
    命名规范：冒号分层（session:{id}:window）—— 便于按前缀批量查/管理，
    这也是 Redis 社区的通用约定（类比文件路径分层）。

    ⚠ 这里的 f-string 会把 uuid.UUID 渲染成**带横杠**的规范形态。
    所以必须保证调用方传进来的 id 已经是规范形态 —— 这正是 D14 把
    chat.py 的 `uuid4().hex`（32 位无横杠）收口成 `uuid4()` 的原因：
    PG 会把无横杠写法规范化后存库，从库里读出来的 id 拼出的 key
    与当初写进去的那个不同一个，热窗口就会静默读不到。
    """
    return f"session:{session_id}:window"


def _to_uuid(session_id: uuid.UUID | str) -> uuid.UUID | None:
    """
    把 id 归一成 uuid.UUID；不是 uuid 形态则返回 None。

    为什么需要它：sessions.id 是 PG 的 uuid 类型，兜底查询必须传 uuid；
    而本模块的历史调用方（如 d8 的"纯 Redis"用例）用的是普通字符串 id。
    对这类 id，PG 里不可能存在对应会话，兜底无从谈起 → 返回 None 让它跳过。
    """
    if isinstance(session_id, uuid.UUID):
        return session_id
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, AttributeError, TypeError):
        return None


async def get_window(session_id: uuid.UUID | str) -> list[dict]:
    """
    读取该会话的滑窗历史消息（两级读取：Redis 优先，PG 兜底）。

    读取顺序：
        ① Redis 命中 → 直接返回（快路径，PG 全程不被碰）
        ② Redis 未命中 / 数据损坏 → 从 PG 取最近 N 轮并**回填 Redis**（D15）
        ③ Redis **不可用** → 同 ②（D15 容错）

    为什么 ③ 不需要单独写分支：`_redis_get` 把连接异常吞成 None，
    于是「Redis 里没有这个 key」与「问不到 Redis」在调用方看来是同一件事 ——
    没有可信的热数据。把两种故障收敛成一条路径，是"少一个分支就少一处漏判"。

    ⚠ 第 ② 步为什么是「读 + 写回」而不是「只读出来返回」：
    append_turn 内部复用的就是本函数（见 append_turn 第 ① 步），所以**只读不写回不会丢历史** ——
    实测证据见 `scripts/d15_backfill_verify.py` 的 B 段对照实验：
    在没有回填的情况下，append_turn 照样能读到 16 条完整历史并原样写回。
    回填的真正收益是两条：
      ① **避免同一轮查两次 PG**：本函数一次 + append_turn 内部一次（实测该路径下确实是两次）
      ② **让热窗口尽早收敛回权威源**：本轮即使中途失败（LLM 超时/异常），
         下一次请求也能直接命中，而不必再兜底查一次库
    这也是缓存 read-repair 的标准做法：缓存被读过之后，就应该与源头保持一致。

    Returns:
        消息列表（时间正序）；无历史时返回空列表（不抛异常，保证对话可用）
    """
    raw = await _redis_get(_window_key(session_id))
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 数据损坏 → 这份缓存不可信：往下走兜底重建，而不是当作"无历史"返回空
            logger.warning("滑窗数据解析失败，将从 PG 重建 session=%s", session_id)

    return await _rebuild_from_pg(session_id)


async def _rebuild_from_pg(session_id: uuid.UUID | str) -> list[dict]:
    """
    PG 兜底：从 messages 表取最近 N 轮「对话语义」，并回填 Redis 热窗口（D15 / PRD F5.3）。

    两个设计选择（都别省）：
      ① **非 uuid 的 id 直接跳过**：sessions.id 是 uuid 类型，非 uuid 形态的 id
         在 PG 里不可能存在，查是白查。顺带也保住了 d8 那个"纯 Redis 用例"的语义。
      ② **只回填非空结果**：PG 也没有历史时（全新会话），不往 Redis 写空数组 ——
         避免把「PG 此刻也没有」这个瞬时结论固化成窗口内容。
    """
    sid = _to_uuid(session_id)
    if sid is None:
        return []

    # 延迟导入：记忆层（热）依赖持久层（冷）这条方向要保持单向。
    # 写成函数内导入，将来 session_service 若反过来需要记忆层也不会变成循环导入。
    from app.services.session_service import get_recent_dialogue

    history = await get_recent_dialogue(sid, turns=WINDOW_TURNS)
    if not history:
        return []

    # 写失败也无所谓（_redis_set 只记日志不抛）：历史已经在返回值里，
    # 本次对话照常进行，下次未命中时会再重建一次。
    await _redis_set(
        _window_key(session_id),
        json.dumps(history, ensure_ascii=False),
        ex=WINDOW_TTL,
    )
    logger.info(
        "热窗口未命中，已从 PG 回填 session=%s 条数=%d", session_id, len(history)
    )
    return history


async def append_turn(
    session_id: uuid.UUID | str, user_content: str, assistant_content: str
) -> None:
    """
    写入一轮对话（Upsert：读 → 合并 → 裁剪 → 整体写回）。

    Args:
        session_id: 会话 ID
        user_content: 用户这轮说的话
        assistant_content: Agent 这轮的回答
    """
    # ① 读出当前窗口。
    #    ⚠ 这里复用 get_window（而不是直接读 Redis）：所以它自带 PG 兜底 ——
    #    Redis 是空的也不会把本轮写成"只剩 1 轮"的残缺窗口。
    #    换句话说，「回填」不是这个函数的正确性前提（见 get_window 的说明）。
    window = await get_window(session_id)
    window.append({"role": "user", "content": user_content})        # ② 追加本轮
    window.append({"role": "assistant", "content": assistant_content})
    window = window[-MAX_MESSAGES:]                # ③ 裁剪：只留最近 N 条（滑窗）

    # ④ 整体写回（ensure_ascii=False 让中文可读，不转成 \uXXXX）
    #    ⚠ 写失败**不抛异常**（D15 容错）：对话已经跑完、答案已经返回，
    #    此时抛 500 只会让用户白等一轮；而这一轮的内容 PG 里有全量（第 ⑧ 步），
    #    丢的只是"热"，不是"数据"。自愈时机见 _redis_set 的说明。
    await _redis_set(
        _window_key(session_id),
        json.dumps(window, ensure_ascii=False),
        ex=WINDOW_TTL,                             # TTL：24 小时后自动过期
    )
    logger.info(
        "记忆已更新 session=%s 窗口长度=%d/%d",
        session_id,
        len(window),
        MAX_MESSAGES,
    )


async def clear_window(session_id: uuid.UUID | str) -> None:
    """
    清空某会话的记忆（用户点"新对话"时用）：热窗口 + 摘要游标一起删。

    为什么游标也要删：窗口和游标描述的是同一段对话的状态 ——
    窗口清了、游标还指着"上次摘到第 120 条"，下一轮摘要就会从那之后算起，
    凭空跳掉一段。两者必须同时归零。

    ⚠ 这里**不碰** `sessions.summary`（它在 PG 里，是另一个存储层的资产）。
    所以本函数目前只适用于"会话内容要保留、只是热数据作废"的场景；
    真要做"清空这个会话"，还得同时清 PG 的 summary —— 等有调用方时再定。
    """
    await _redis_delete(_window_key(session_id))
    await _redis_delete(_summary_cursor_key(session_id))
    logger.info("记忆已清空 session=%s", session_id)


# ============================================================
# 摘要压缩（D15 第二段 / PRD F5.1 后半 + F5.2 + §9.4）
# ============================================================
def _format_dialogue(rows: list[dict]) -> str:
    """把消息行拼成给 LLM 读的纯文本（"用户：…／助手：…"）。"""
    return "\n".join(
        f"{'用户' if r['role'] == 'user' else '助手'}：{r['content']}" for r in rows
    )


async def maybe_summarize(session_id: uuid.UUID | str) -> dict:
    """
    检查是否该做摘要压缩；该做就做一次。每次调用**最多摘一批**。

    Returns:
        {"summarized": bool, "reason": str, "merged": int}
        reason 取值：below_trigger / nothing_slid_out / below_min_batch /
                     llm_failed / empty_output / ok

    完整判断链（顺序即优先级）：
        ① 从 **PG** 数轮数 —— 少于等于 20 轮直接返回（PRD F5.2 的阈值）
        ② 算热窗口下界 —— 为 None 说明还没有消息滑出窗口，没有可摘的区间
        ③ 读游标 → 取区间 `(游标, 窗口下界)` 内的消息
        ④ 不足 4 条（2 轮）先攒着 —— 见下面的频率取舍
        ⑤ 交给 LLM 压缩（旧摘要 + 本段新内容），写回 `sessions.summary`
        ⑥ 推进游标到本批最后一条的 id

    触发频率的取舍（4 条 = 攒够 2 轮再摘）：
        每次摘都会让 LLM **改写一遍旧摘要**，改写越频繁，细节丢失越多（代际漂移）。
        攒 2 轮再摘把调用次数砍一半，代价是"刚滑出窗口的那 1 轮"暂时不在摘要里
        （最多 1 轮的缺口，下一批补上）。而摘要本身就是压缩、细节必然丢，
        所以这 1 轮的暂时缺口影响很小 —— 换来的是更稳的摘要 + 更少的调用。

    为什么每次只摘一批（而不是 while 循环摘到追平）：
        本函数在请求链路里同步执行，一次 LLM 调用约 1-3 秒。循环摘会把某一次
        请求的延迟无限拉长。单批上限 40 条（20 轮）已经能覆盖主路径（稳态下
        每 2 轮才攒出 4 条）；只有"游标丢失后的追赶"或"长会话首次摘要"才会
        出现多批，那种情况让它在后续几轮里逐步追平即可 —— 摘要暂时落后是
        可接受的降级，请求延迟失控不是。

    为什么同步执行、不丢后台任务：
        同步 = 行为可预测、可断言、失败当场可见（学习阶段这点更重要）。
        代价是每约 2 轮多 1-3 秒延迟。将来要异步化，把它换成
        `asyncio.create_task` 或独立 worker 即可，本函数的输入输出不用改。
    """
    from app.prompts.summarizer import SUMMARY_SYSTEM_PROMPT, build_summary_user_prompt
    from app.services.llm_gateway import chat
    from app.services.session_service import (
        get_dialogue_range,
        get_dialogue_stats,
        get_summary,
        save_summary,
    )

    sid = _to_uuid(session_id)
    if sid is None:
        return {"summarized": False, "reason": "not_a_uuid", "merged": 0}

    # ① 轮数从 PG 数（Redis 窗口永远数不出 20 轮）
    stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
    turns = stats["total"] // 2           # 一轮 = user + assistant = 2 条
    if turns <= SUMMARY_TRIGGER_TURNS:
        return {"summarized": False, "reason": "below_trigger", "merged": 0}

    # ② 窗口下界：为 None 说明没有消息滑出窗口（理论上与 ① 同时不成立，仍显式判断）
    lower = stats["window_lower_id"]
    if lower is None:
        return {"summarized": False, "reason": "nothing_slid_out", "merged": 0}

    # ③ 待摘区间 = (游标, 窗口下界)
    cursor = await _get_cursor(session_id)
    rows = await get_dialogue_range(
        sid,
        after_id=cursor,
        before_id=lower,
        limit=SUMMARY_BATCH_MESSAGES,
    )

    # ④ 攒够才摘
    if len(rows) < SUMMARY_MIN_MESSAGES:
        return {"summarized": False, "reason": "below_min_batch", "merged": 0}

    # ⑤ 压缩（增量合并：旧摘要 + 本段）
    previous = await get_summary(sid)
    try:
        new_summary = await chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_summary_user_prompt(previous, _format_dialogue(rows)),
                },
            ],
            trace_name="memory-summarize",
            temperature=SUMMARY_TEMPERATURE,
        )
    except Exception:
        # 摘要失败不影响对话（回答早已返回）。不推进游标 → 下一轮会重试同一段，
        # 所以这里吞掉异常是安全的：不会"跳过一段没摘的内容"。
        logger.exception("摘要生成失败（对话不受影响）session=%s", session_id)
        return {"summarized": False, "reason": "llm_failed", "merged": 0}

    new_summary = (new_summary or "").strip()
    if not new_summary:
        # 空输出同样不推进游标：下一轮重试。save_summary 也会拒绝空串（双保险）
        logger.warning("摘要返回空内容，已跳过 session=%s", session_id)
        return {"summarized": False, "reason": "empty_output", "merged": 0}

    # ⑥ 先落摘要、再推游标 —— 顺序不能反。
    #    反过来的话，若写摘要失败而游标已推进，这一段内容就**永久不会**再被摘要
    #    （游标说"摘过了"，可摘要里没有）→ 静默丢失记忆。
    #    当前顺序下，最坏情况是"摘要写了、游标没推进"→ 下一轮重摘一次，只是多花一次调用。
    await save_summary(sid, new_summary)
    await _set_cursor(session_id, rows[-1]["id"])

    logger.info(
        "摘要已更新 session=%s 本批条数=%d 游标=%s 摘要长度=%d",
        session_id,
        len(rows),
        rows[-1]["id"],
        len(new_summary),
    )
    return {"summarized": True, "reason": "ok", "merged": len(rows)}
