"""
租户上下文（"当前这次请求属于谁"）
====================================
多租户隔离里最容易被漏掉的一环：**检索链路**。

为什么不能像其它接口那样把 `owner_id` 当参数一路传下去：
  调用链是  chat/api → agent_service → LLM → **工具调用**（`execute_tool`）
            → `app/tools/retrieval.py` → `retrieval_service.retrieve()`
  中间那一跳是**由 LLM 决定的**：它只负责填 `query` 这个参数，
  不可能、也不应该填 `owner_id`（它连自己是谁都不知道，让它填等于让它越权）。
  所以"我是谁"必须是**环境信息**，不是函数参数。

  这就是 ContextVar 的用途：在请求入口处 `with owner_scope(user.id):` 设一次，
  链路上任何深度都能读到，退出时自动还原。
  它同时解决了并发问题 —— ContextVar 是**每个 asyncio Task 一份**的，
  两个用户同时对话时各自的上下文不会串（区别于"一个模块级全局变量"那种写法，
  那样在高并发下必然把 A 的数据查给 B）。

  类比前端：React 的 Context —— 不用把 user 一层层 prop drilling 下去，
  但作用域仍然是**组件树**（这里是 Task）内的，不是全局的。

⚠ 一条纪律：**没有上下文 = 不过滤**（返回 None）。
  这**不是**安全默认值，而是给"系统内部调用"留的口子：
  迁移脚本、评测运行、预热脚本没有登录用户。因此：
    · 接口层必须保证：凡是能读到用户数据的地方，都先设好了作用域；
    · 需要"系统视角"的地方（评测）显式 `owner_scope(admin_id)`，
      而不是靠"忘了设置"来获得全局视野。
  两者在代码里长得一样，所以在 review 时看的是**入口有没有设**。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 默认 None = 没有活动用户（系统内部调用）
_current_owner: ContextVar[uuid.UUID | None] = ContextVar("current_owner", default=None)


def current_owner() -> uuid.UUID | None:
    """当前请求的归属用户；系统内部调用（无请求上下文）时为 None。"""
    return _current_owner.get()


@contextmanager
def owner_scope(owner_id: uuid.UUID | None) -> Iterator[None]:
    """
    在 with 块内把"当前用户"设为 owner_id，退出时**还原成进入前的值**。

    为什么必须还原（而不是 `set(None)`）：
      嵌套场景是真实存在的 —— 一次请求里可能先设了用户 A，内部又为某个
      系统任务临时切到 admin，切完必须回到 A。写死 None 会让外层剩余部分
      突然失去归属，症状是"同一次请求里前一半查得到、后一半查不到"。
    用 token + reset 是 ContextVar 的标准做法，也是唯一正确的做法。
    """
    token = _current_owner.set(owner_id)
    try:
        yield
    finally:
        _current_owner.reset(token)


def owner_filter(column, owner_id: uuid.UUID | None = None):
    """
    生成一条 SQLAlchemy 过滤条件：`column == owner_id`，或"不过滤"。

    为什么返回"条件"而不是"在调用处写 if"：
      调用处写 if 会出现两种分支各写一遍 SQL 的情况，改一处漏一处。
      这里返回一个**恒真条件**（`true`）让 SQL 形状保持一致 ——
      查询计划可以复用，代码也只有一条路径要看。

    ⚠ 注意 owner_id 为 None 时返回的是"不过滤"，见文件头的纪律说明。
    """
    from sqlalchemy import true

    target = owner_id if owner_id is not None else current_owner()
    if target is None:
        return true()
    return column == target
