"""
用户画像（两张表：显式字段 + 隐式事实）
========================================
一行画像 = 一组**跨会话稳定**的、关于"这个用户是谁"的事实与偏好。

判据很硬（写代码时反复用得到）：**换一个新会话，它还必须成立**。
  · "我叫老郭，做前端" → 是画像（下周开新会话仍然成立）
  · "这个函数为什么报错" → 不是画像（是本次会话的上下文）

这条判据同时解释了它为什么**不能**塞进 `sessions.summary`：
那张表一行对应一个会话，天生是会话级的作用域，跨不了会话。

为什么拆两张表而不是一张
------------------------
显式字段（`user_profiles.explicit`）与隐式事实（`user_profile_facts`）的
**信任等级、编辑方式、注入配额**全都不同：

| 维度 | 显式（用户自己填） | 隐式（LLM 从对话里抽） |
|---|---|---|
| 谁说的算 | 用户 | 模型 |
| 改的方式 | 表单保存，改了就生效 | 只能删，改要重新抽 |
| 注入策略 | **全量**（短、且任何场景都相关） | 按本轮问题取 top-k |
| 出错后果 | 用户自己填错的，他能改 | 模型猜错的，用户会觉得"这系统坏了" |

挤在一张表里，早晚要按"来源"字段到处分支 —— 那正是两张表该拆的信号。

为什么隐式事实要存 `text_hash`
------------------------------
唯一约束 `(owner_id, text_hash)` 是**防重复累积**的唯一手段。
抽取是反复跑的（每 6 轮一次），同一句"偏好 TypeScript"会被抽出来很多次；
没有这个约束，一个月后一个用户的画像里会有 20 条意思相同的话，
而 top-k 只能取 4 条 —— 等于把**有限的注入配额**浪费在重复上。

⚠ `text_hash` 存的是**规范化后**文本的 sha256（见 profile_service.normalize_fact_text）：
  规范化 = 去首尾空白、全角转半角、压缩连续空白、去掉句末标点。
  不规范化的话，"偏好 TypeScript。" 与 "偏好 TypeScript" 会被当成两条。

为什么 `source_session_id` 是 SET NULL 而不是 CASCADE
----------------------------------------------------
删掉一个会话，不该连带删掉从它里面抽出来的**画像**。
画像的语义是"关于这个人的长期事实"，它一旦成立就独立于来源存在 ——
来源只是"为什么这么认为"的追溯线索，线索没了不影响事实本身。
反过来用 CASCADE 会有个很难发现的事故：用户清理历史会话 → 画像悄悄清空 →
他会以为"这系统记不住我"，而实际是被级联删了。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
# ⚠ `text` 必须**起别名**导入。
#   UserProfileFact 里有一个业务字段就叫 `text`（"一句话陈述"），
#   而 Python 的类体是一个普通作用域：`text: Mapped[str] = mapped_column(...)`
#   这一行执行完，类体内的名字 `text` 就被绑定成了 MappedColumn 对象，
#   后面再写 `server_default=sa_text("0.7")` 会调到一个不可调用对象上，报
#   `TypeError: 'MappedColumn' object is not callable`。
#   起别名是唯一彻底的解法（换个字段名只是把这个坑推给下一个改代码的人）。
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# ---- 隐式事实的种类（唯一出处：抽取 prompt、接口校验、前端展示都从这里取）----
FACT_KINDS: tuple[str, ...] = ("preference", "fact", "goal")
FACT_PREFERENCE = "preference"
FACT_FACT = "fact"
FACT_GOAL = "goal"

# ---- 显式字段的固定 schema（唯一出处）----
# 结构：(key, 中文标签, 最大长度, 占位提示)
#
# 为什么是固定 4 个而不是自由 key-value：
#   ① 前端能画成确定的表单，而不是一个"键值对编辑器"（后者没人愿意填）
#   ② 后端能校验长度与取值，自由 key-value 只能校验"是个字符串"
#   ③ 注入 prompt 时能按固定顺序拼，输出稳定
# 代价是"用户想记的东西不在字段里" —— 这部分交给隐式事实补，两条腿各管一段。
EXPLICIT_FIELDS: tuple[tuple[str, str, int, str], ...] = (
    ("nickname", "称呼", 32, "希望 Agent 怎么称呼你，例如：老郭"),
    ("occupation", "职业 / 身份", 64, "例如：前端工程师"),
    ("tech_stack", "技术栈", 96, "例如：TypeScript、React、Node.js"),
    ("answer_style", "回答风格", 128, "例如：直接给结论，少铺垫；代码要能直接跑"),
)

EXPLICIT_KEYS: tuple[str, ...] = tuple(f[0] for f in EXPLICIT_FIELDS)


class UserProfile(Base):
    """一人一行的**显式**画像（用户自己填的那部分）。"""

    __tablename__ = "user_profiles"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # 用户被删 → 画像一起删。这里用 CASCADE 是对的（与事实表的 source_session_id 相反）：
        # 画像的归属就是这个人，人没了，画像没有独立存在的意义。
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="归属用户（一人一行，所以直接做主键）",
    )
    explicit: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::jsonb"),
        comment="用户自填字段，键取自 EXPLICIT_KEYS；空值不写入（不是存空串）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<UserProfile owner={self.owner_id} fields={sorted((self.explicit or {}).keys())}>"


class UserProfileFact(Base):
    """一条**隐式**事实（LLM 从对话里抽出来的，一条一行）。"""

    __tablename__ = "user_profile_facts"
    __table_args__ = (
        # 防重复累积的核心约束（见文件头）。放数据库而不是应用层：
        # 抽取是后台任务，可能并发跑两份，应用层"先查再插"挡不住。
        UniqueConstraint("owner_id", "text_hash", name="uq_profile_facts_owner_hash"),
        # 列表页的查询是"某人的事实，按时间倒序"
        Index("ix_profile_facts_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="归属用户",
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FACT_FACT,
        comment="preference | fact | goal（取值见 FACT_KINDS）",
    )
    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="一句话陈述，如「偏好 TypeScript 而不是 JavaScript」",
    )
    text_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="规范化文本的 sha256（去重依据，见文件头）",
    )
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL：删会话不删画像（见文件头）
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        comment="从哪次会话抽出来的（可追溯「它为什么这么认为」；会话删除后置空）",
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.7,
        server_default=sa_text("0.7"),
        comment="抽取时模型自评的可信度（0~1）；仅用于排序与展示，不参与任何自动删除",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<UserProfileFact owner={self.owner_id} kind={self.kind} text={self.text[:20]!r}>"
