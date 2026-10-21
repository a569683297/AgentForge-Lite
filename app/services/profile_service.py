"""
用户画像服务（读写 + 去重 + top-k 匹配 + 渲染）
==============================================
一句话职责：**把"这个用户是谁"变成一段可以拼进 system prompt 的文本**，
以及让用户能看见、能改、能删。

四段能力，按调用方向排列：

  写（显式）  update_explicit   —— 用户在画像页填的表单
  写（隐式）  add_facts         —— 后台抽取器交来的一批事实（自动去重）
  读          get_profile       —— 注入用；**失败不抛，返回空画像**
  删          delete_fact       —— 用户说"这条不对"

为什么读要 fail-open（失败就返回空），写要分两种
------------------------------------------------
这是本项目里最容易搞混的一处，先说清楚：

| 场景 | 失败时 | 理由 |
|---|---|---|
| 对话中读画像 | **返回空，对话继续** | 画像读不到 = "这次没有个性化"，不是"不能对话" |
| 抽取后写事实 | **只记 warning，不抛** | 抽取是后台任务，失败下轮再试；绝不能让抽画像把对话搞挂 |
| 用户点保存 | **抛，让前端看到错误** | 用户点了保存就必须知道"到底存没存上"，静默成功是最坏的 |

与认证的 fail-closed（读不到白名单就拒绝）正好相反。判据是**这一层是不是业务的前置条件**：
认证是（不知道你是谁就不能放行），画像是（不知道你是谁也能回答，只是没那么贴心）。

关于 top-k 匹配为什么先用关键词而不是向量
--------------------------------------------
向量更准，但代价是三样：多一张 embedding 表、多一次 embedding 调用（约 +30ms）、
以及一条"画像更新后要重建索引"的运维约束。而画像事实很短（一句 20~40 字），
关键词重合的召回已经够用 —— 用户问"React 怎么写 hook"时，
"正在准备 React 面试"这条事实里的 "React" 一定命中。

先上关键词、把链路和页面跑通；等真的出现"答得不贴"的反馈，再换成向量。
**不提前为一个尚未观测到的问题付复杂度代价。**
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.user_profile import (
    EXPLICIT_FIELDS,
    EXPLICIT_KEYS,
    FACT_FACT,
    FACT_KINDS,
    UserProfile,
    UserProfileFact,
)

# ---- 注入配额与读取上限（调优参数，放模块常量与 memory_service 的摘要参数同族）----
FACT_TOP_K = 4              # 隐式事实最多注入几条
FACT_LIST_LIMIT = 100       # 单次从库里最多取多少条参与匹配（按时间倒序取最近的）
FACT_MAX_LEN = 200          # 单条事实的最大长度（超长截断，防止一条事实挤爆 prompt）
EXPLICIT_VALUE_MAX = {key: max_len for key, _label, max_len, _hint in EXPLICIT_FIELDS}


@dataclass(frozen=True)
class FactItem:
    """一条隐式事实（对外形状，与 ORM 解耦 —— 接口层只认这个）。"""

    id: int
    kind: str
    text: str
    confidence: float
    source_session_id: uuid.UUID | None
    created_at: str | None


@dataclass(frozen=True)
class Profile:
    """一个用户的完整画像。`is_empty` 为真时调用方应跳过整个画像段。"""

    explicit: dict[str, str] = field(default_factory=dict)
    facts: list[FactItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.explicit and not self.facts


class ProfileValidationError(ValueError):
    """显式字段校验失败（未知键 / 超长）。接口层翻成 422。"""


# ============================================================
# 规范化与哈希（去重的唯一依据）
# ============================================================
# 为什么必须规范化之后再哈希：
#   抽取器每次输出的措辞会有细微差别（"偏好 TypeScript。" vs "偏好 TypeScript"），
#   直接哈希等于没去重。规范化做四件事，都是**不改变语义**的：
#     ① NFKC 归一（全角转半角，'Ｔｙｐｅ' → 'Type'）
#     ② 压缩连续空白
#     ③ 去掉首尾空白
#     ④ 去掉句末标点（。．.！!？?）
#   ⚠ 不做同义词替换、不做大小写折叠（英文技术名词大小写有意义：React ≠ react 在展示上，
#     虽然检索上等价 —— 这里宁可能多留一条，也不要合并掉两条意思不同的事实）。
_TRAILING_PUNCT = re.compile(r"[\s。．.!！?？；;，,、]+$")
_SPACES = re.compile(r"\s+")


def normalize_fact_text(text: str) -> str:
    """规范化一条事实文本（去重与展示都用这个形态）。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = _SPACES.sub(" ", s).strip()
    return _TRAILING_PUNCT.sub("", s)


def fact_hash(text: str) -> str:
    """规范化文本的 sha256（16 进制，64 字符）。"""
    return hashlib.sha256(normalize_fact_text(text).encode("utf-8")).hexdigest()


# ============================================================
# 读
# ============================================================
def _to_fact_item(row: UserProfileFact) -> FactItem:
    return FactItem(
        id=row.id,
        kind=row.kind,
        text=row.text,
        confidence=float(row.confidence or 0.0),
        source_session_id=row.source_session_id,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


async def get_profile(owner_id: uuid.UUID | None) -> Profile:
    """
    读一个用户的完整画像。**任何异常都返回空画像，不向上抛。**

    fail-open 的理由见模块 docstring 的表格。这里额外解释一个容易走偏的点：
    捕获 `Exception` 而不是只捕 `SQLAlchemyError` —— 表不存在（迁移没跑）、
    连接池耗尽、asyncpg 协议错误，都会在**首次上线**时以各种形态出现，
    而它们共同的处理方式是一样的（降级为空 + 记 warning）。
    分成两类写只会让第一条漏网的异常把对话打成 500。

    ⚠ 只记 warning 不记 exception：画像读不到是"已降级"，
      与 memory_service 的 Redis 降级同一个判断（见那里的长注释）。
    """
    if owner_id is None:
        return Profile()

    try:
        async with async_session_factory() as db:
            profile_row = await db.get(UserProfile, owner_id)
            fact_rows = (
                await db.execute(
                    select(UserProfileFact)
                    .where(UserProfileFact.owner_id == owner_id)
                    # 只取最近的 N 条参与匹配：一个人的事实正常在几十条内，
                    # 但抽取器万一失控（比如 prompt 改了），这条上限能兜住内存与延迟。
                    .order_by(UserProfileFact.created_at.desc())
                    .limit(FACT_LIST_LIMIT)
                )
            ).scalars().all()
    except Exception as e:  # noqa: BLE001 —— 见 docstring
        logger.warning("读取用户画像失败，已降级为空画像 owner=%s err=%s", owner_id, e)
        return Profile()

    explicit = {
        key: str(value).strip()
        for key, value in (profile_row.explicit if profile_row else {}).items()
        if key in EXPLICIT_KEYS and str(value).strip()
    }
    return Profile(explicit=explicit, facts=[_to_fact_item(r) for r in fact_rows])


async def list_facts(owner_id: uuid.UUID, *, limit: int = FACT_LIST_LIMIT) -> list[FactItem]:
    """
    列出某人的事实（画像页用；不做相关性过滤）。
    这里**不吞异常**：页面拿不到数据就该看到错误，而不是一片空白假装"没有画像"。
    """
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(UserProfileFact)
                .where(UserProfileFact.owner_id == owner_id)
                .order_by(UserProfileFact.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [_to_fact_item(r) for r in rows]


# ============================================================
# 写 · 显式字段
# ============================================================
def validate_explicit(values: dict[str, str | None]) -> dict[str, str]:
    """
    校验并清洗用户提交的显式字段。

    两条规则：
      ① **未知键直接拒绝**，不是忽略 —— 忽略会让前端拼错字段名时"保存成功但没生效"，
         用户反复保存都改不动，而接口一直返回 200。
      ② 空值/纯空白 = **删除这个字段**（不存空串）。存空串会让 render 时
         出现"职业：" 这样的空行，模型会以为这个信息存在但为空。
    """
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in EXPLICIT_KEYS:
            raise ProfileValidationError(f"未知的画像字段：{key}")
        text = (value or "").strip()
        if not text:
            continue
        limit = EXPLICIT_VALUE_MAX[key]
        if len(text) > limit:
            raise ProfileValidationError(f"字段 {key} 最长 {limit} 个字符（当前 {len(text)}）")
        clean[key] = text
    return clean


async def update_explicit(owner_id: uuid.UUID, values: dict[str, str | None]) -> dict[str, str]:
    """
    整体替换用户的显式字段（PATCH 语义 = 提交什么就是什么）。

    为什么是**整体替换**而不是逐字段合并：
      前端是一个表单，用户清空某个输入框的意图就是"删掉这个字段"。
      合并语义下"清空"与"没提到"无法区分，于是清空永远不生效 ——
      这是表单类接口最经典的一个坑。
    """
    clean = validate_explicit(values)

    async with async_session_factory() as db:
        stmt = (
            pg_insert(UserProfile)
            .values(owner_id=owner_id, explicit=clean)
            .on_conflict_do_update(
                index_elements=[UserProfile.owner_id],
                set_={"explicit": clean},
            )
        )
        await db.execute(stmt)
        await db.commit()

    logger.info("画像显式字段已更新 owner=%s 字段=%s", owner_id, sorted(clean))
    return clean


# ============================================================
# 写 · 隐式事实
# ============================================================
async def add_facts(
    owner_id: uuid.UUID,
    facts: list[dict],
    *,
    source_session_id: uuid.UUID | None = None,
) -> int:
    """
    批量写入事实，**重复的静默跳过**（靠 (owner_id, text_hash) 唯一约束）。

    Args:
        facts: [{"kind": "preference", "text": "...", "confidence": 0.8}, ...]
    Returns:
        实际新增条数（被去重掉的、非法的都不计入）

    为什么用 `ON CONFLICT DO NOTHING` 而不是"先查再插"：
      抽取是后台任务，完全可能两份并发跑（比如用户手快发了两轮触发两次）。
      先查再插在并发下会双双查到"不存在"然后双双插入 —— 唯一约束是最后一道闸，
      而**在应用层做去重判断，等于把这道闸绕过去**。

    为什么非法的单条只跳过、不整批失败：
      一批 5 条里 1 条 kind 拼错，因此丢掉另外 4 条正确的 —— 那是过度反应。
    """
    if not facts:
        return 0

    rows: list[dict] = []
    seen_hashes: set[str] = set()
    for item in facts:
        text = normalize_fact_text(str(item.get("text") or ""))
        if not text:
            continue
        if len(text) > FACT_MAX_LEN:
            text = text[:FACT_MAX_LEN]
        kind = str(item.get("kind") or FACT_FACT)
        if kind not in FACT_KINDS:
            kind = FACT_FACT
        h = fact_hash(text)
        # 同一批内部先去重：ON CONFLICT 管得住跨批，但同一批里两条完全相同的
        # 记录会让 INSERT ... VALUES 自己撞上自己（PG 报 "cannot affect row a second time"）。
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        try:
            confidence = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        rows.append(
            {
                "owner_id": owner_id,
                "kind": kind,
                "text": text,
                "text_hash": h,
                "source_session_id": source_session_id,
                "confidence": min(max(confidence, 0.0), 1.0),
            }
        )

    if not rows:
        return 0

    async with async_session_factory() as db:
        stmt = (
            pg_insert(UserProfileFact)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["owner_id", "text_hash"])
        )
        result = await db.execute(stmt)
        await db.commit()

    inserted = int(result.rowcount or 0)
    logger.info(
        "画像事实写入 owner=%s 提交=%d 新增=%d（去重/丢弃 %d）",
        owner_id,
        len(rows),
        inserted,
        len(rows) - inserted,
    )
    return inserted


async def delete_fact(owner_id: uuid.UUID, fact_id: int) -> bool:
    """
    删一条事实。返回 False 表示"这个 id 对这个人不存在"。

    ⚠ 归属条件写在 DELETE 的 WHERE 里，**不是"先查再删"**：
      先查再删在两条语句之间有一个窗口（虽然很小），更重要的是
      "查到了 → 判断归属 → 删"这段逻辑只要有人改漏一步就是越权删除。
      一条带 owner_id 的 DELETE 不给这个错误留位置。

    接口层把 False 翻成 **404 而不是 403**（403 会泄露"这个 id 存在"）。
    """
    from sqlalchemy import delete

    async with async_session_factory() as db:
        result = await db.execute(
            delete(UserProfileFact).where(
                UserProfileFact.id == fact_id,
                UserProfileFact.owner_id == owner_id,
            )
        )
        await db.commit()
    return bool(result.rowcount)


# ============================================================
# 相关性选择（top-k）
# ============================================================
def _contains_any(token: str, others: set[str]) -> bool:
    """
    token 是否与 others 里某个词有**包含关系**（任一方向）。

    ⚠ 这是实测逼出来的，不是预防性设计：
      写测试时用 `前端` 去匹配事实 `前端开发相关的第 N 条事实`，一条都没命中 ——
      因为 jieba 把「前端开发」切成**一个词**，与「前端」不相等。
      这类"词表里有复合词"的情况在中文里非常普遍（前端开发 / 机器学习 / 大模型应用），
      只做完全相等匹配会让画像在最常见的问法上**静默失效**。

    为什么限制 len >= 2：
      单字（"的""和""写"）是任何词的子串，放进来会让所有事实都"命中" ——
      那等于匹配失效，而且看不出任何异常（分数都 > 0，排序变成近似随机）。
    """
    if len(token) < 2:
        return False
    for other in others:
        if len(other) < 2:
            continue
        if token in other or other in token:
            return True
    return False


def _count_hits(query_tokens: set[str], fact_tokens: set[str]) -> int:
    """命中词数（完全相等 ∪ 包含关系）。"""
    hits = 0
    for token in query_tokens:
        if token in fact_tokens or _contains_any(token, fact_tokens):
            hits += 1
    return hits


def _bool_cosine(query_tokens: set[str], fact_tokens: set[str]) -> float:
    """
    布尔余弦：命中词数 / sqrt(|query| × |fact|)。

    为什么不用 `命中数 / |query|`（纯覆盖率）：
      覆盖率对长事实完全不惩罚 —— 一条 200 字的事实只要碰巧含一个查询词，
      得分就和一条精准命中的 8 字事实一样。分母带 |fact| 就是在惩罚"靠长度蹭分"。

    为什么不用 Jaccard：
      Jaccard 的分母是并集，会对**长查询**过度惩罚（用户问一句话 20 个词，
      命中 3 个画像词，Jaccard 只有 0.15，看起来完全不相关）。
      余弦是两个集合各自归一后再乘，量纲上更合理。
    """
    if not query_tokens or not fact_tokens:
        return 0.0
    hit = _count_hits(query_tokens, fact_tokens)
    if hit == 0:
        return 0.0
    return hit / math.sqrt(len(query_tokens) * len(fact_tokens))


def select_relevant(
    facts: list[FactItem], query: str, *, top_k: int = FACT_TOP_K
) -> list[FactItem]:
    """
    从事实里挑出与本轮问题最相关的 top_k 条。

    选择规则（顺序即优先级）：
      ① 分词后算布尔余弦，>0 的按分数降序取 top_k
      ② 一条都不相关时，**按 confidence 取满 top_k**（不超过事实总数）兜底

    为什么 ② 要兜底：
      用户的很多提问（"帮我看下这段代码"）与画像字面上毫无重合，
      但"他是前端工程师、偏好直接给结论"仍然应该影响回答风格。
      完全不带任何事实，会让画像在**最需要它的时候（泛泛的提问）恰好消失**。

    为什么 ② 是"取满 top_k"而不是"只取 1 条"（2026-10-21 真实故障后修正）：
      零相关只说明「匹配没有给出信号」，**不等于「每条事实都同样无关」**。
      此时没有任何依据去挑某一条，正确做法是「按 confidence 排序、填满预算」。
      原实现只取 1 条，理由是"多带一条只是噪音"，但真实场景是：
      用户画像只有 2 条（"是开发工程师" / "希望被称呼为梦"，confidence 同为 0.95），
      问「我叫什么」时两条余弦都是 0 → 只挑 id 小的那条 → **名字被丢掉**，
      Agent 答"我没有你的名字信息"。
      事实总量小于 top_k 时，"只取 1 条"等于无理由丢弃一半画像，
      而身份/称呼类问题恰恰是画像最该有用的时候。
      噪声的代价（多几十 token、弱相关事实可能影响措辞）
      远小于"画像在最该出现的时刻整条消失"。

    为什么分词复用 `tokenizer.tokenize`：
      它是全项目唯一的分词出口（D16 的红线）。画像匹配另起一套分词，
      会导致"同一个词在两处切法不同"，而这类不一致从来不报错，只是效果变差。
    """
    if not facts or top_k <= 0:
        return []

    from app.services.tokenizer import tokenize

    # 零信号兜底：空查询与"一条都不相关"走同一条规则 —— 按 confidence 取满 top_k。
    # 两者本质相同（匹配没给出任何依据），分成两个不同的配额只会多一个不一致点。
    def _by_confidence(limit: int) -> list[FactItem]:
        return sorted(facts, key=lambda f: (-f.confidence, f.id))[:limit]

    query_tokens = set(tokenize(query))
    if not query_tokens:
        return _by_confidence(top_k)

    scored: list[tuple[float, FactItem]] = []
    for fact in facts:
        score = _bool_cosine(query_tokens, set(tokenize(fact.text)))
        if score > 0:
            scored.append((score, fact))

    if not scored:
        return _by_confidence(top_k)

    scored.sort(key=lambda pair: (-pair[0], -pair[1].confidence, pair[1].id))
    return [fact for _score, fact in scored[:top_k]]


# ============================================================
# 渲染（拼进 system prompt）
# ============================================================
def render_profile(explicit: dict[str, str], facts: list[FactItem]) -> str | None:
    """
    把画像渲染成一段文本；都为空时返回 None（调用方据此跳过整段）。

    两段分开写，是因为它们的**可信度不同**，要分别告诉模型：
      · 显式：用户自己填的 → 当作事实
      · 隐式：模型从对话里抽的 → 可能过时/抽错，冲突时以用户当前说法为准

    最后那句约束（"不要在回答里主动复述"）不是可选的润色：
      不加的话模型会开始说"记得你上次提到……"，而画像里根本没有"上次"的时序，
      它只是在复述一段背景文本 —— 听起来像编造，实际是提示词诱导出来的行为。
    """
    parts: list[str] = []

    if explicit:
        lines = []
        for key, label, _max_len, _hint in EXPLICIT_FIELDS:
            value = explicit.get(key)
            if value:
                lines.append(f"- {label}：{value}")
        if lines:
            parts.append("【用户档案】（用户本人填写）\n" + "\n".join(lines))

    if facts:
        lines = [f"- {f.text}" for f in facts]
        parts.append("【过往对话中了解到的信息】（可能不准确或已过时）\n" + "\n".join(lines))

    if not parts:
        return None

    return (
        "\n\n".join(parts)
        + "\n\n使用这些信息时：\n"
        "1. 只用来调整举例、措辞与深浅，不要在回答里主动复述（不要说「我记得你……」）。\n"
        "2. 【用户档案】是用户本人填写的，可信度更高；【过往对话中了解到的信息】"
        "由系统自动提取，可能过时或有误。两者冲突时，以【用户档案】为准。\n"
        "3. 与用户当前的说法冲突时，以当前说法为准。"
    )


def build_profile_block(profile: Profile, query: str) -> str | None:
    """从完整画像里选出本轮该注入的部分并渲染。注入链路的唯一入口。"""
    if profile.is_empty:
        return None
    facts = select_relevant(profile.facts, query)
    return render_profile(profile.explicit, facts)


async def render_for_prompt(owner_id: uuid.UUID | None, query: str) -> str | None:
    """
    对话链路的**唯一**调用点：读画像 + 选相关 + 渲染，一步到位。

    为什么再包一层（而不是让 agent_service 自己串三行）：
      `get_profile` 的 fail-open 语义、`select_relevant` 的 top-k、
      `render_profile` 的使用约束，这三件事的**组合顺序**是有讲究的
      （先读、再选、最后渲染，顺序错了就会把全部事实都塞进去）。
      包成一个函数，调用方就不会只抄到前两步。
    """
    if owner_id is None:
        return None
    profile = await get_profile(owner_id)
    return build_profile_block(profile, query)


# ============================================================
# 调试辅助（脚本 / 测试用）
# ============================================================
def dumps(profile: Profile) -> str:
    """把画像打成 JSON 字符串（排查"为什么这轮没带上画像"时用）。"""
    return json.dumps(
        {
            "explicit": profile.explicit,
            "facts": [asdict(f) for f in profile.facts],
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )
