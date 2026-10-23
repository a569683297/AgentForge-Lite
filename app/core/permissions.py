"""
权限点（Permission）与角色映射
==============================

一条原则：**后端是权限的唯一真相，前端只负责"不把走不通的路摆出来"。**

为什么需要这一层（而不是继续到处写 `role == "admin"`）：
  `role` 回答的是"你是谁"，`permission` 回答的是"你能做什么"。
  现在只有 user / admin 两个角色，两者恰好等价 —— 于是 `role == "admin"`
  看起来够用。但一旦出现第三种角色（比如"只读运营"），这个判断会在几十个
  地方**同时失效**，而且失效方式是**静默的**（少判一个 if 不报错，只是权限
  悄悄放开了）。把判定收敛成"角色 → 权限集合"的一处映射，加角色就是加一行。

为什么权限点用**代码常量**而不是数据库表：
  · 权限点必须与代码里的 `require_permission(...)` 一一对应。放 DB 里意味着
    代码可以引用一个 DB 里不存在的权限点 —— 那不是"灵活"，是"拼错字符串
    也不报错"，且要到线上才发现。
  · 现阶段角色固定，DB 表带来的唯一好处是"不发版就能改"，而项目还没有
    "在线改角色权限"的运营需求；代价却是一张表 + 一套管理界面 + 缓存一致性。
  · 将来真要做"自定义角色"，改的是 `permissions_for_role` 的内部实现
    （查 DB 而不是查常量），**所有调用点都不用动**。这才是这层抽象的价值：
    变化的边界被收在一个函数里。

为什么 admin 是**显式列出全部权限**，而不是通配 `*`：
  通配有两个代价：① 新增权限点会**自动**给 admin，于是没人会 review
  "这个新能力该不该给管理员"；② 安全审计时无法回答"admin 到底能干什么"，
  因为答案藏在"全部减去某几个"里。显式列出 = 这张表本身就是权限清单，
  可以直接对着它数。

为什么未知角色返回**空集**而不是全给或报错：
  新增角色忘了配映射时，结果是"什么都干不了"——用户会立刻报错、问题当场暴露；
  反过来（默认放行）则是"谁都能干"，可能几个月没人发现。安全默认值必须 fail-closed。
"""

from __future__ import annotations

from typing import Mapping

from app.models.user import ROLE_ADMIN, ROLE_USER

# ============================================================
# 权限点目录
# ============================================================
# 命名规则：`<资源>.<动作>`，全小写、点分隔。
# 按**功能**划分而不是按接口划分 —— 一个页面可能调多个接口，
# 一个接口也可能被多个页面用；按功能划，前端菜单/路由才能直接对应到权限点。

# ---- 对话 ----
PERM_CHAT_USE = "chat.use"

# ---- 知识库（数据本身还用 owner_id 做行级隔离，这里是"能不能用这个功能"）----
PERM_DOC_READ = "doc.read"
PERM_DOC_WRITE = "doc.write"

# ---- 我的画像 ----
PERM_PROFILE_READ = "profile.read"
PERM_PROFILE_WRITE = "profile.write"

# ---- 模型配置（llm_models 带 owner_id，是**用户私有**资源）----
PERM_MODEL_READ = "model.read"
PERM_MODEL_WRITE = "model.write"

# ---- 工具 ----
# ⚠ 这里刻意分成两个：
#   `tools.list`  = 对话时能拿到工具清单（LLM 要据此决定调什么）——普通用户必须有，
#                   否则对话功能直接废掉；
#   `tools.manage`= 工具注册表管理页（增删 MCP server、改配置）——管理员专属。
#   如果合成一个，"能对话"和"能改全局工具配置"就被绑死了，必然有一边是错的。
PERM_TOOLS_LIST = "tools.list"
PERM_TOOLS_MANAGE = "tools.manage"

# ---- 检索检查器 ----
PERM_RETRIEVER_USE = "retriever.use"

# ---- 平台级观测（看的是**全平台**数据，不是自己的）----
PERM_EVAL_READ = "eval.read"
PERM_TRACE_READ = "trace.read"
PERM_METRICS_READ = "metrics.read"

# ---- 账号与审计 ----
PERM_ADMIN_USERS = "admin.users"
PERM_ADMIN_AUDIT = "admin.audit"

# 保序元组：文档、前端展示、测试遍历都以它为准
ALL_PERMISSIONS: tuple[str, ...] = (
    PERM_CHAT_USE,
    PERM_DOC_READ,
    PERM_DOC_WRITE,
    PERM_PROFILE_READ,
    PERM_PROFILE_WRITE,
    PERM_MODEL_READ,
    PERM_MODEL_WRITE,
    PERM_TOOLS_LIST,
    PERM_TOOLS_MANAGE,
    PERM_RETRIEVER_USE,
    PERM_EVAL_READ,
    PERM_TRACE_READ,
    PERM_METRICS_READ,
    PERM_ADMIN_USERS,
    PERM_ADMIN_AUDIT,
)

# ============================================================
# 角色 → 权限
# ============================================================
_USER_PERMISSIONS: frozenset[str] = frozenset(
    {
        PERM_CHAT_USE,
        PERM_DOC_READ,
        PERM_DOC_WRITE,
        PERM_PROFILE_READ,
        PERM_PROFILE_WRITE,
        PERM_MODEL_READ,
        PERM_MODEL_WRITE,
        PERM_TOOLS_LIST,
        PERM_RETRIEVER_USE,
    }
)

# 管理员 = 目录里的全部（显式展开，理由见文件头）
_ADMIN_PERMISSIONS: frozenset[str] = frozenset(ALL_PERMISSIONS)

ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = {
    ROLE_USER: _USER_PERMISSIONS,
    ROLE_ADMIN: _ADMIN_PERMISSIONS,
}


# ============================================================
# 查询
# ============================================================
def permissions_for_role(role: str | None) -> frozenset[str]:
    """
    该角色拥有的权限集合。未知角色 / None → 空集（fail-closed，理由见文件头）。

    归一化（strip + lower）放在这里而不是指望调用方：
    角色值来自数据库与 token，大小写不一致的历史数据不该让判定静默失效。
    """
    return ROLE_PERMISSIONS.get((role or "").strip().lower(), frozenset())


def has_permission(role: str | None, permission: str) -> bool:
    """单点判定。**所有**权限检查都应走这里，不要在调用处自己拼集合。"""
    return permission in permissions_for_role(role)


# ============================================================
# 模块级自检：配置错误在 import 时就炸，而不是等到某次请求
# ============================================================
# 这三条检查防的都是"改了常量但没改映射"这类静默错误：
#   · 给角色配了一个不存在的权限点（拼错）→ 该权限永远拿不到，且没有任何报错；
#   · ALL_PERMISSIONS 里重复 → 前端菜单去重后少一项，排查起来毫无头绪。
def _self_check() -> None:
    if len(set(ALL_PERMISSIONS)) != len(ALL_PERMISSIONS):
        raise RuntimeError("ALL_PERMISSIONS 存在重复项")

    catalog = set(ALL_PERMISSIONS)
    declared: set[str] = set()
    for role, perms in ROLE_PERMISSIONS.items():
        unknown = set(perms) - catalog
        if unknown:
            raise RuntimeError(f"角色 {role} 配置了未声明的权限点：{sorted(unknown)}")
        declared |= set(perms)

    # admin 必须是全集：这条断言把"新增权限点忘了给管理员"变成启动即失败，
    # 而不是"管理员莫名其妙少了某个入口"这种要用户来报的 bug
    if declared != catalog:
        raise RuntimeError(
            f"有权限点没有任何角色拥有：{sorted(catalog - declared)}"
        )

    for perm in ALL_PERMISSIONS:
        if "." not in perm or perm != perm.strip().lower():
            raise RuntimeError(f"权限点命名不合规（应为小写 <资源>.<动作>）：{perm}")


_self_check()
