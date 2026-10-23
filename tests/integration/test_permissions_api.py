"""
集成：权限清单的下发与逐接口拦截
================================

`test_permissions.py` 测的是"角色 → 权限点"这张表本身；
这个文件测的是**它有没有真的被用上**，两者的失败后果不一样：

  表算错了  → 前端菜单不对，但后端可能仍然放行（安全洞）
  表没被用上 → 表看起来全对，实际谁都能进（更隐蔽）

所以这里的每条用例都是一次真实 HTTP 调用，断言的是状态码 ——
不看前端、不看菜单，只问"拿着这个身份的 token，这个接口让不让我过"。

三个必测的性质：

1. **登录与 /me 都要带 permissions**
   前端靠它决定显示什么。少一个入口就表现为"菜单莫名其妙少了几项"，
   而服务端日志一切正常 —— 这种故障没有报错，只能靠用例钉住。

2. **普通用户打平台级接口必须 403**（不是"藏了入口"）
   前端藏菜单只是体验；谁都能直接 curl。

3. **角色改动立刻生效**（不用重新登录）
   `get_current_user` 每请求回查 users 表，所以降级/升级在下一个请求就生效。
   如果哪天有人为了"省一条查询"改成只信 token claims，这条用例会红。
"""

from __future__ import annotations

import uuid

import pytest

from app.core.permissions import ALL_PERMISSIONS

pytestmark = pytest.mark.db

# 平台级（管理员）接口的代表样本。每个文件挑一两条即可 ——
# 逐个接口断言的价值不如"每个权限点至少被验证一次"。
ADMIN_ONLY_ENDPOINTS = (
    "/api/admin/users",
    "/api/admin/audit",
    "/api/admin/audit/actions",
    "/api/eval/dataset",
    "/api/traces",
    "/api/metrics",
    "/api/tool-servers",
)

# 普通用户必须能用的基础接口（缺了它们，产品对普通用户就废了）
BASIC_ENDPOINTS = (
    "/api/documents",
    "/api/tools",
    "/api/profile",
    "/api/sessions",
)


async def _make_user(role: str = "user"):
    """直接建一个临时账号（不走注册接口：这里不验注册，也不该付 166ms 的 bcrypt）。"""
    from app.core.db import async_session_factory
    from app.core.security import hash_password
    from app.models.user import User

    email = f"pytest-perm-{uuid.uuid4().hex[:8]}@agentforge.local"
    async with async_session_factory() as session:
        user = User(
            email=email,
            password_hash=hash_password("PytestPass123", rounds=4),
            display_name="perm",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _token(user) -> str:
    from app.core.security import TOKEN_TYPE_ACCESS, create_token

    token, _, _ = create_token(user.id, token_type=TOKEN_TYPE_ACCESS, role=user.role)
    return token


# ============================================================
# 一、权限清单的下发
# ============================================================
async def test_login_returns_permissions_for_admin(anon_client, admin_user):
    """登录响应必须带 permissions，且管理员是全集。"""
    response = await anon_client.post(
        "/api/auth/login",
        json={"email": admin_user.email, "password": "PytestPass123"},
    )
    assert response.status_code == 200, response.text
    perms = response.json()["user"]["permissions"]
    assert set(perms) == set(ALL_PERMISSIONS)


async def test_login_returns_permissions_for_plain_user(anon_client, normal_user):
    """
    普通用户的清单是**非空的、且不含平台级权限**。

    断言"非空"很重要：如果 `issue_tokens` 退回直接塞 ORM 对象，
    permissions 会落到默认空列表 —— 前端会表现为"菜单全没了"，
    而登录接口仍然 200，没有任何报错。
    """
    response = await anon_client.post(
        "/api/auth/login",
        json={"email": normal_user.email, "password": "PytestPass123"},
    )
    assert response.status_code == 200, response.text
    perms = set(response.json()["user"]["permissions"])

    assert perms, "普通用户的权限清单不能为空"
    assert "chat.use" in perms
    assert "admin.users" not in perms
    assert "eval.read" not in perms


async def test_me_returns_permissions(api_client, user_client):
    """`/me` 也要带 —— 前端每次启动靠它刷新权限（角色改了不必重新登录）。"""
    admin_perms = (await api_client.get("/api/auth/me")).json()["permissions"]
    user_perms = (await user_client.get("/api/auth/me")).json()["permissions"]

    assert set(admin_perms) == set(ALL_PERMISSIONS)
    assert "admin.users" not in user_perms


async def test_refresh_keeps_permissions(anon_client, normal_user):
    """
    刷新令牌的返回体同样要带清单。

    前端刷新是**静默的**（在拦截器里做），如果这个入口漏了字段，
    下一次静默刷新就会把菜单清空 —— 用户看到的是"用着用着侧边栏少了一半"。
    """
    login = await anon_client.post(
        "/api/auth/login",
        json={"email": normal_user.email, "password": "PytestPass123"},
    )
    refresh_token = login.json()["refresh_token"]

    refreshed = await anon_client.post(
        "/api/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["user"]["permissions"], "刷新后权限清单不能变空"


# ============================================================
# 二、逐接口拦截
# ============================================================
@pytest.mark.parametrize("path", ADMIN_ONLY_ENDPOINTS)
async def test_plain_user_gets_403_on_admin_endpoints(user_client, path):
    """普通用户访问平台级接口：403，且 detail 里指出缺哪个权限点（便于排障）。"""
    response = await user_client.get(path)
    assert response.status_code == 403, f"{path} 竟然返回 {response.status_code}"
    assert "权限不足" in response.json()["detail"]


@pytest.mark.parametrize("path", ADMIN_ONLY_ENDPOINTS)
async def test_admin_is_not_blocked(api_client, path):
    """
    管理员不该被挡（403）。这里**只断言不是 403**：
    部分接口依赖 Langfuse / MCP 等外部服务，缺它们时会是 500 或降级 ——
    那是环境问题，不是权限问题，混在一起会让这条用例变成"看心情红"。
    """
    response = await api_client.get(path)
    assert response.status_code != 403, f"{path} 把管理员挡在了外面"


@pytest.mark.parametrize("path", BASIC_ENDPOINTS)
async def test_plain_user_keeps_basic_endpoints(user_client, path):
    """
    普通用户的基础功能不能被权限点误伤。

    这是权限收紧时最容易出的事故：为了关掉一个管理页，把 `doc.read`
    从 user 里一起删了 —— 管理页确实关了，知识库也一起没了。
    """
    response = await user_client.get(path)
    assert response.status_code == 200, f"{path} 把普通用户挡住了：{response.text}"


async def test_anonymous_still_gets_401(anon_client):
    """没有凭证是 401（不是 403）—— 前端靠这个状态码决定跳登录。"""
    response = await anon_client.get("/api/admin/users")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").startswith("Bearer")


# ============================================================
# 三、角色改动立刻生效
# ============================================================
async def test_role_change_takes_effect_on_next_request():
    """
    同一条 token：角色改成 admin 后，下一个请求就能进管理接口。

    这条守的是 `deps.get_current_user` 的"每请求回查库"。
    如果改成只信 token 里的 role claims，这里会一直是 403（要等 token 过期），
    而管理员在后台点了"升级"，对方却要等 2 小时才生效 —— 用户只会觉得"点了没用"。
    """
    from httpx import ASGITransport, AsyncClient

    from app.core.db import async_session_factory
    from app.main import app
    from app.models.user import User

    user = await _make_user("user")
    # token 里签的是 role=user
    token = _token(user)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        assert (await client.get("/api/admin/users")).status_code == 403

        # 库里升级 —— **不动 token**
        async with async_session_factory() as session:
            row = await session.get(User, user.id)
            row.role = "admin"
            await session.commit()

        assert (await client.get("/api/admin/users")).status_code == 200

        # 再降回普通用户，同一个 token 立刻又不能进
        async with async_session_factory() as session:
            row = await session.get(User, user.id)
            row.role = "user"
            await session.commit()

        assert (await client.get("/api/admin/users")).status_code == 403
