"""
集成：管理员权限与审计日志
===========================
这个文件守三类东西，它们的失败后果完全不同：

**一、权限分层**（`403` 而不是"藏了入口"）
  前端藏一个菜单项不等于没权限 —— 谁都能直接 curl。
  所以每条"管理员专属"都真的打一次，断言 403。
  `/api/tools` 这条特别值得一提：它原来没有任何凭证依赖（诊断用），
  账号体系上线后成了唯一一个"匿名就能列出全部工具名与 MCP server 状态"的口子，
  现在收到登录之后（普通用户即可 —— 对话页要用它）。

**二、自锁防护**（`400`）
  管理员把自己禁用 / 把最后一个管理员降级，会让整个平台进入"只能改库"的状态。
  这两条规则在接口层强制执行，不依赖前端。

**三、审计真的写了**（不是"有个表"）
  一次状态变更必须留下"谁、对谁、从什么变成什么"。
  对外不区分登录失败原因，对内**必须**区分 —— 所以登录失败的用例查的是 `detail.reason`。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.db


# ============================================================
# 造数与清理
# ============================================================


async def _make_user(role: str = "user"):
    """直接建一个临时账号（不走注册接口：这里不验注册，也不该付 bcrypt 的 166ms）。"""
    from app.core.db import async_session_factory
    from app.core.security import hash_password
    from app.models.user import User

    email = f"pytest-adminapi-{uuid.uuid4().hex[:8]}@agentforge.local"
    async with async_session_factory() as session:
        user = User(
            email=email,
            password_hash=hash_password("PytestPass123", rounds=4),
            display_name="pytest",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _drop_user(user_id) -> None:
    from sqlalchemy import delete

    from app.core.db import async_session_factory
    from app.models.user import User

    async with async_session_factory() as session:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


async def _drop_audit_of(email: str) -> None:
    """
    清掉本次用例造的审计行。

    ⚠ 只在测试里这么做。生产环境删审计 = 抹掉痕迹，
      而审计的全部价值就在于"删不掉"（接口层根本没给删除入口）。
    """
    from sqlalchemy import delete

    from app.core.db import async_session_factory
    from app.models.audit_log import AuditLog

    async with async_session_factory() as session:
        await session.execute(delete(AuditLog).where(AuditLog.actor_email == email))
        await session.commit()


# ============================================================
# 一、权限分层
# ============================================================

# 管理员专属（普通用户 403）
_ADMIN_ONLY = [
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/audit"),
    ("GET", "/api/admin/audit/actions"),
    ("GET", "/api/tool-servers"),
    ("GET", "/api/eval/cases"),
    ("GET", "/api/metrics"),
    ("GET", "/api/traces"),
]


@pytest.mark.parametrize("method,path", _ADMIN_ONLY)
async def test_normal_user_gets_403_on_admin_only(user_client, method, path):
    """
    ★ 普通用户打管理员接口 → 403。

    为什么是 403 而不是 404（与"别人的文档 → 404"不同）：
      这里的资源是**平台级**的，不是某个人的私有数据 —— 它确实存在，
      只是你不能用。而"别人的文档"连存在性都不该确认。
      两种处理看起来不一致，其实是两条不同的信息边界。
    """
    resp = await user_client.request(method, path)
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/sessions"),
        ("GET", "/api/documents"),
        ("GET", "/api/models"),
        ("GET", "/api/tools"),
        ("GET", "/api/retriever/options"),
        ("GET", "/api/auth/me"),
    ],
)
async def test_anonymous_gets_401(anon_client, method, path):
    """没带凭证 → 401（带 WWW-Authenticate，前端据此知道"该去登录了"）。"""
    resp = await anon_client.request(method, path)
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


async def test_normal_user_can_use_own_features(user_client):
    """反向断言：收得太紧也是 bug —— 普通用户该能用的必须能用。"""
    for path in ("/api/documents", "/api/models", "/api/tools", "/api/retriever/options"):
        assert (await user_client.get(path)).status_code == 200, path


# ============================================================
# 二、自锁防护
# ============================================================


async def test_admin_cannot_disable_self(api_client, admin_user):
    """
    ★ 管理员禁用自己 → 400。

    没有这条的话，一个误操作就能让平台失去唯一的入口 ——
    而 `scripts/auth_set_password.py` 只改密码，不改 is_active，
    真锁死了只能上宿主机改库。
    """
    resp = await api_client.patch(
        f"/api/admin/users/{admin_user.id}", json={"is_active": False}
    )
    assert resp.status_code == 400
    assert "不能禁用当前登录的账号" in resp.json()["detail"]

    # 自己还是能用的（没有"先改了再报错"这种半途状态）
    assert (await api_client.get("/api/auth/me")).status_code == 200


async def test_last_admin_cannot_be_demoted(api_client, admin_user):
    """
    ★ 只有一个管理员时，降级自己 → 400。

    造条件的方式：把库里**其他** admin 临时降成 user，测完还原。
    只有这样才能真正走到"remaining == 0"那一支 —— 库里只要有第二个 admin，
    这条规则就是允许的（那才是正常操作）。
    """
    from sqlalchemy import select, update

    from app.core.db import async_session_factory
    from app.models.user import ROLE_USER, User

    async with async_session_factory() as session:
        others = (
            await session.scalars(
                select(User.id).where(User.role == "admin", User.id != admin_user.id)
            )
        ).all()
        if others:
            await session.execute(
                update(User).where(User.id.in_(others)).values(role=ROLE_USER)
            )
            await session.commit()

    try:
        resp = await api_client.patch(
            f"/api/admin/users/{admin_user.id}", json={"role": "user"}
        )
        assert resp.status_code == 400
        assert "最后一个管理员" in resp.json()["detail"]
    finally:
        if others:
            async with async_session_factory() as session:
                await session.execute(
                    update(User).where(User.id.in_(others)).values(role="admin")
                )
                await session.commit()


async def test_promote_then_demote_other_user(api_client, admin_user):
    """
    正常操作必须放行：把别人提成 admin、再降回 user（因为还有我这个 admin 在）。

    ⚠ 这条与上面的"最后一个管理员"是**同一段代码的两侧**。
      只测拒绝会让人以为规则是"不许降级"，而它真正说的只是"不许降到一个不剩"。
    """
    target = await _make_user("user")
    try:
        promoted = await api_client.patch(
            f"/api/admin/users/{target.id}", json={"role": "admin"}
        )
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "admin"

        demoted = await api_client.patch(
            f"/api/admin/users/{target.id}", json={"role": "user"}
        )
        assert demoted.status_code == 200
        assert demoted.json()["role"] == "user"
    finally:
        await _drop_user(target.id)


async def test_patch_unknown_user_returns_404(api_client):
    resp = await api_client.patch(
        f"/api/admin/users/{uuid.uuid4()}", json={"is_active": False}
    )
    assert resp.status_code == 404


async def test_empty_patch_is_rejected(api_client, admin_user):
    """两个字段都不传 → 422（"什么也不改"的 PATCH 多半是前端写错了）。"""
    resp = await api_client.patch(f"/api/admin/users/{admin_user.id}", json={})
    assert resp.status_code == 422


# ============================================================
# 三、审计
# ============================================================


async def test_actions_catalog_matches_model_constants(api_client):
    """
    前端筛选下拉的来源必须与后端的动作常量一致。

    这条守的是"加了新动作但忘了登记"——症状是那个动作**永远筛不出来**，
    而列表页看起来一切正常。
    """
    from app.models.audit_log import ACTIONS

    resp = await api_client.get("/api/admin/audit/actions")
    assert resp.status_code == 200
    assert resp.json()["actions"] == list(ACTIONS)


async def test_disable_user_is_audited(api_client, admin_user):
    """
    ★ 一次状态变更 → 一条审计，且带"从什么变成什么"。

    只记"谁禁用了谁"是不够的：半年后有人问"这个号是谁、什么时候恢复过"，
    没有 from/to 就答不上来。
    """
    target = await _make_user("user")
    try:
        resp = await api_client.patch(
            f"/api/admin/users/{target.id}", json={"is_active": False}
        )
        assert resp.status_code == 200

        logs = (
            await api_client.get(
                "/api/admin/audit",
                params={"action": "admin.user_disabled", "actor_id": str(admin_user.id), "limit": 50},
            )
        ).json()["items"]

        mine = [row for row in logs if row["target_id"] == str(target.id)]
        assert mine, "禁用账号没有留下审计记录"
        entry = mine[0]
        assert entry["actor_email"] == admin_user.email
        assert entry["detail"]["to"] is False
        assert entry["detail"]["email"] == target.email
    finally:
        await _drop_user(target.id)
        await _drop_audit_of(admin_user.email)


async def test_failed_login_is_audited_with_reason(api_client):
    """
    ★ 对外"邮箱或密码错误"，对内必须能分清是哪种错。

    三种 reason 的价值完全不同：
      user_not_found → 有人在扫邮箱
      bad_password   → 撞库
      inactive       → 有人拿已禁用的号在试
    混成一条，运营就没法判断该封 IP 还是该排查泄露。
    """
    email = f"pytest-ghost-{uuid.uuid4().hex[:8]}@agentforge.local"
    try:
        resp = await api_client.post(
            "/api/auth/login", json={"email": email, "password": "WhateverPass1"}
        )
        assert resp.status_code == 401

        logs = (
            await api_client.get(
                "/api/admin/audit",
                params={"action": "auth.login_failed", "limit": 50},
            )
        ).json()["items"]

        mine = [row for row in logs if row["actor_email"] == email]
        assert mine, "登录失败没有留下审计记录"
        assert mine[0]["detail"]["reason"] == "user_not_found"
        # 用户不存在时 actor_id 必须是空 —— 不能编一个 id 出来
        assert mine[0]["actor_id"] is None
    finally:
        await _drop_audit_of(email)


async def test_audit_is_filterable_by_actor(api_client, admin_user):
    """按 actor 过滤：审计页最常用的入口就是"这个人做过什么"。"""
    resp = await api_client.get(
        "/api/admin/audit", params={"actor_id": str(admin_user.id), "limit": 20}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= len(body["items"])
    for row in body["items"]:
        assert row["actor_id"] == str(admin_user.id)
