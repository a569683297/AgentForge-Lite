"""
权限目录的单测（app/core/permissions.py）
==========================================

这一层没有 IO、没有数据库，看起来"看一眼就知道对不对" —— 但它决定了
**谁能做什么**。所以这里的用例不测"函数能跑"，而是逐条钉住那些
"改错了也不会报错、只会在某天被用户发现"的性质：

· 每个角色配的权限点必须是目录里真实存在的（拼错一个字符 → 该权限永远拿不到）
· admin 必须是全集（新增权限点忘了给管理员 → 管理页莫名少一项）
· 未知角色必须是空集（fail-closed：新增角色忘了配映射 → 什么都干不了，立刻暴露）
"""

from __future__ import annotations

import pytest

from app.core.permissions import (
    ALL_PERMISSIONS,
    PERM_ADMIN_AUDIT,
    PERM_ADMIN_USERS,
    PERM_CHAT_USE,
    PERM_DOC_READ,
    PERM_EVAL_READ,
    PERM_METRICS_READ,
    PERM_MODEL_WRITE,
    PERM_PROFILE_READ,
    PERM_RETRIEVER_USE,
    PERM_TOOLS_LIST,
    PERM_TOOLS_MANAGE,
    PERM_TRACE_READ,
    ROLE_PERMISSIONS,
    has_permission,
    permissions_for_role,
)
from app.models.user import ROLE_ADMIN, ROLE_USER


# ============================================================
# 目录本身
# ============================================================
def test_all_permissions_are_unique():
    assert len(set(ALL_PERMISSIONS)) == len(ALL_PERMISSIONS)


def test_permission_names_follow_convention():
    """`<资源>.<动作>`、全小写。命名不一致本身不会报错，只会让前端对不上号。"""
    for perm in ALL_PERMISSIONS:
        assert "." in perm, f"{perm} 缺少点分隔"
        assert perm == perm.strip().lower(), f"{perm} 不是小写/含空白"
        resource, _, action = perm.partition(".")
        assert resource and action, f"{perm} 缺少资源段或动作段"


def test_every_permission_is_granted_to_some_role():
    """孤儿权限点（没有任何角色拥有）= 写了但永远不可能通过的检查，属于死配置。"""
    granted = set().union(*ROLE_PERMISSIONS.values())
    assert granted == set(ALL_PERMISSIONS)


def test_role_permissions_only_reference_declared_permissions():
    """防拼错：`"eval.raed"` 这种字符串在集合里不会报错，只会静默不生效。"""
    declared = set(ALL_PERMISSIONS)
    for role, perms in ROLE_PERMISSIONS.items():
        assert set(perms) <= declared, f"{role} 引用了未声明的权限点"


# ============================================================
# 角色 → 权限
# ============================================================
def test_admin_has_every_permission():
    """
    管理员必须是**全集**。

    这条用例的价值不在"现在对不对"，而在下次加权限点时：
    如果你只把新权限给了 user 而忘了 admin，它会当场红 ——
    否则表现是"管理员点进某页发现少了按钮"，且没人想得到是权限表漏了一行。
    """
    assert permissions_for_role(ROLE_ADMIN) == frozenset(ALL_PERMISSIONS)


def test_plain_user_has_basic_permissions():
    perms = permissions_for_role(ROLE_USER)
    for perm in (
        PERM_CHAT_USE,
        PERM_DOC_READ,
        PERM_PROFILE_READ,
        PERM_MODEL_WRITE,
        PERM_TOOLS_LIST,
        PERM_RETRIEVER_USE,
    ):
        assert perm in perms, f"普通用户缺少基础权限 {perm}"


def test_plain_user_has_no_platform_admin_permissions():
    """平台级观测与管理能力一律不给普通用户 —— 这是权限体系的核心断言。"""
    perms = permissions_for_role(ROLE_USER)
    for perm in (
        PERM_EVAL_READ,
        PERM_TRACE_READ,
        PERM_METRICS_READ,
        PERM_TOOLS_MANAGE,
        PERM_ADMIN_USERS,
        PERM_ADMIN_AUDIT,
    ):
        assert perm not in perms, f"普通用户不应拥有 {perm}"


@pytest.mark.parametrize("role", [None, "", "  ", "superuser", "ADMINISTRATOR", "adminx"])
def test_unknown_or_empty_role_gets_nothing(role):
    """
    fail-closed：未知角色 → 空集。

    如果这里返回全集（"默认放行"），那么一个新角色在配好映射之前
    就已经能访问一切 —— 而且没有任何报错。空集则是"立刻不能用"，会被当场发现。
    """
    assert permissions_for_role(role) == frozenset()


def test_role_lookup_is_case_and_space_insensitive():
    """角色值来自库与 token，历史数据大小写不一致不该让判定静默失效。"""
    assert permissions_for_role(" Admin ") == permissions_for_role(ROLE_ADMIN)
    assert permissions_for_role("USER") == permissions_for_role(ROLE_USER)


# ============================================================
# 单点判定
# ============================================================
def test_has_permission_for_admin_and_user():
    assert has_permission(ROLE_ADMIN, PERM_EVAL_READ) is True
    assert has_permission(ROLE_USER, PERM_EVAL_READ) is False
    assert has_permission(ROLE_USER, PERM_CHAT_USE) is True


def test_has_permission_rejects_undeclared_permission():
    """
    未声明的权限点**任何人都不该通过**（包括 admin）。

    如果实现写成"admin 直接 return True"，那么代码里一个拼错的权限点
    会让管理员畅通无阻、普通用户永远 403 —— 两条路都不会报错。
    """
    assert has_permission(ROLE_ADMIN, "does.not.exist") is False
    assert has_permission(ROLE_USER, "does.not.exist") is False
