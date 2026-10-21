"""
集成：账号接口（真打 FastAPI + 真 Postgres + 真 Redis）
=======================================================
这里验的不是"接口能不能返回 200"，而是四条**安全不变量**：

  ① 响应原文里永远没有 `password_hash`（连字段名都不该出现）
  ② 登录失败对外不区分原因：用户不存在 / 密码错 / 账号被禁 → 同一句话、同一个码
  ③ refresh **轮换**：换出来的不是同一个串，旧的立刻作废
  ④ 改密之后，改密前签发的 refresh 全部失效

两条写用例的纪律，都是为了"失败时能看懂原因"：

1. **每个用例自己建账号，自己删**
   绝不拿 `pytest-user` / `pytest-admin` 去改密码 —— 那会把共享夹具改坏，
   后续用例会以"密码不对"的形式红在另一个文件里，真正的原因却在这里。

2. **限流用例必须清 Redis 键**
   登录失败 5 次会锁 5 分钟。测完不清理，同邮箱的后续用例会撞上"被锁"，
   而它看起来和"密码错了"一模一样。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.db


# ============================================================
# 夹具与工具
# ============================================================


async def _delete_user(email: str) -> None:
    """删掉一个临时账号。顺带把它名下的数据带走（外键 CASCADE）。"""
    from sqlalchemy import delete

    from app.core.db import async_session_factory
    from app.models.user import User

    async with async_session_factory() as session:
        await session.execute(delete(User).where(User.email == email))
        await session.commit()


async def _set_active(email: str, active: bool) -> None:
    """直接改库里的 is_active —— 用它造"被禁用的账号"，比走接口少一层依赖。"""
    from sqlalchemy import update

    from app.core.db import async_session_factory
    from app.models.user import User

    async with async_session_factory() as session:
        await session.execute(
            update(User).where(User.email == email).values(is_active=active)
        )
        await session.commit()


@pytest.fixture
async def accounts():
    """
    收集本用例建的临时邮箱，**用例结束统一删除**。

    为什么用夹具而不是 try/finally 散在每个用例里：
      用例中途 assert 失败时 finally 仍会跑，但一旦有人新写用例时忘了写 finally，
      库里就开始堆孤儿账号。夹具让"清理"变成默认行为，忘不掉。
    """
    created: list[str] = []
    yield created
    for email in created:
        await _delete_user(email)


async def _register(
    anon_client, accounts: list[str], prefix: str, password: str = "PytestPass123"
) -> dict:
    """注册一个临时账号，返回响应体 + 补上 `_email` / `_password` 两个键。"""
    email = f"pytest-{prefix}-{uuid.uuid4().hex[:8]}@agentforge.local"
    resp = await anon_client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    body["_email"] = email
    body["_password"] = password
    accounts.append(email)   # 立刻登记：后面即使 assert 失败也会被清理
    return body


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 一、注册
# ============================================================


async def test_register_returns_token_pair_without_password_hash(anon_client, accounts):
    """
    注册成功直接给 token（不让用户再登一次），且响应里**不得**出现哈希。

    `"password_hash" not in resp.text` 这条断言比逐字段检查更狠：
      它连"藏在某个嵌套对象里"也能抓到。schema 已经把字段拿掉了，
      这条是给"将来有人手滑把 ORM 对象整个塞进响应"准备的。
    """
    body = await _register(anon_client, accounts, "reg")

    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    # 库里早有别的用户（夹具建的），所以新注册的必然是普通用户
    assert body["user"]["role"] == "user"
    assert body["user"]["is_active"] is True
    assert "password_hash" not in str(body)


async def test_register_duplicate_email_returns_409(anon_client, accounts):
    first = await _register(anon_client, accounts, "dup")

    resp = await anon_client.post(
        "/api/auth/register", json={"email": first["_email"], "password": "AnotherPass123"}
    )

    # 409 Conflict 而不是 400：这两者的区别是"重试也不会成功" vs "改一下再试"
    assert resp.status_code == 409
    assert "已注册" in resp.json()["detail"]


async def test_register_short_password_returns_422(anon_client, accounts):
    email = f"pytest-short-{uuid.uuid4().hex[:8]}@agentforge.local"
    accounts.append(email)

    resp = await anon_client.post(
        "/api/auth/register", json={"email": email, "password": "1234567"}
    )

    # 422 是 pydantic 的 min_length 挡下来的（schema 层），400 是 service 层抛的 ——
    # 密码太短属于"表单填错"，走 422 与项目里其他字段校验一致
    assert resp.status_code == 422


async def test_register_bad_email_returns_422(anon_client):
    resp = await anon_client.post(
        "/api/auth/register", json={"email": "not-an-email", "password": "PytestPass123"}
    )
    assert resp.status_code == 422


async def test_register_normalizes_email_to_lowercase(anon_client, accounts):
    """
    大写邮箱注册 → 存小写。

    这条不测的话会有一个很经典的坑：注册时存了 `Foo@x.com`，
    用户登录时输 `foo@x.com` 就查不到人（PG 的字符串比较区分大小写）。
    """
    email = f"Pytest-Case-{uuid.uuid4().hex[:8]}@AgentForge.Local"
    resp = await anon_client.post(
        "/api/auth/register", json={"email": email, "password": "PytestPass123"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["email"] == email.lower()
    accounts.append(email.lower())


# ============================================================
# 二、登录与当前用户
# ============================================================


async def test_login_then_me_roundtrip(anon_client, accounts):
    created = await _register(anon_client, accounts, "login")

    resp = await anon_client.post(
        "/api/auth/login",
        json={"email": created["_email"], "password": created["_password"]},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    me = await anon_client.get("/api/auth/me", headers=_bearer(token))
    assert me.status_code == 200
    assert me.json()["email"] == created["_email"]
    assert "password_hash" not in me.text


async def test_login_failures_are_indistinguishable(anon_client, accounts):
    """
    ★ 三种失败必须**同一句话**：密码错 / 用户不存在 / 账号被禁用。

    只要有一项不同，这个接口就变成了"批量验证邮箱是否注册过"的工具。
    这里直接比对 detail 字符串 —— 文案一旦被改成不一样的，用例立刻红。
    """
    created = await _register(anon_client, accounts, "enum")

    wrong_password = await anon_client.post(
        "/api/auth/login",
        json={"email": created["_email"], "password": "WrongPassword123"},
    )
    unknown_user = await anon_client.post(
        "/api/auth/login",
        json={"email": f"nobody-{uuid.uuid4().hex[:8]}@agentforge.local", "password": "x"},
    )

    await _set_active(created["_email"], False)
    disabled = await anon_client.post(
        "/api/auth/login",
        json={"email": created["_email"], "password": created["_password"]},
    )
    await _set_active(created["_email"], True)   # 还回去，免得影响删除以外的操作

    assert wrong_password.status_code == unknown_user.status_code == disabled.status_code == 401
    assert (
        wrong_password.json()["detail"]
        == unknown_user.json()["detail"]
        == disabled.json()["detail"]
    )


async def test_login_locks_after_five_failures(anon_client, accounts):
    """
    连续 5 次失败后，**即使密码正确**也进不去（锁 5 分钟）。

    ⚠ 结尾必须清 Redis 计数键：不清的话同邮箱后续用例会撞上锁定期，
      而它长得和"密码错了"完全一样。
    """
    from app.core.redis import redis_client
    from app.services.auth_service import _LOGIN_FAIL_KEY

    created = await _register(anon_client, accounts, "lock")
    key = _LOGIN_FAIL_KEY.format(email=created["_email"])
    try:
        for _ in range(5):
            resp = await anon_client.post(
                "/api/auth/login",
                json={"email": created["_email"], "password": "WrongPassword123"},
            )
            assert resp.status_code == 401

        # 第 6 次用**正确**密码 —— 仍然 401，说明拦截发生在校验之前
        locked = await anon_client.post(
            "/api/auth/login",
            json={"email": created["_email"], "password": created["_password"]},
        )
        assert locked.status_code == 401
        # 文案与"密码错"一致，不告诉攻击者"你已经被锁了"
        assert locked.json()["detail"] == "邮箱或密码错误"
    finally:
        await redis_client.delete(key)


async def test_me_requires_token(anon_client):
    assert (await anon_client.get("/api/auth/me")).status_code == 401


async def test_me_rejects_refresh_token_as_access(anon_client, accounts):
    """
    ★ 类型混用必须被拒：refresh 当 access 用，等于把 7 天的凭证变成 7 天的接口访问权。

    `decode_token(expected_type=...)` 会硬拒绝 —— 这条用例守的就是它。
    """
    created = await _register(anon_client, accounts, "typemix")

    resp = await anon_client.get("/api/auth/me", headers=_bearer(created["refresh_token"]))
    assert resp.status_code == 401


async def test_me_rejects_disabled_account_immediately(anon_client, accounts):
    """
    ★ 禁用**立刻**生效，不等 access 过期（最长 2 小时）。

    实现方式：`get_current_user` 每个请求回查 users 表。
    这条用例就是在守"别为了省一次查询把它优化掉"。
    """
    created = await _register(anon_client, accounts, "disable")
    token = created["access_token"]

    assert (await anon_client.get("/api/auth/me", headers=_bearer(token))).status_code == 200

    await _set_active(created["_email"], False)
    assert (await anon_client.get("/api/auth/me", headers=_bearer(token))).status_code == 401


# ============================================================
# 三、refresh 轮换与登出
# ============================================================


async def test_refresh_rotates_and_old_token_dies(anon_client, accounts):
    """
    ★ 轮换：刷新拿到的是**新** refresh，旧的再刷一次必须 401。

    这是"refresh 被盗后只能被用一次"的实现方式 —— 攻击者用掉之后，
    真实用户的下一次刷新会失败，异常就被暴露出来了。
    """
    created = await _register(anon_client, accounts, "rotate")
    old_refresh = created["refresh_token"]

    first = await anon_client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200
    new_refresh = first.json()["refresh_token"]
    assert new_refresh != old_refresh

    replay = await anon_client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401

    # 新的一直能用（不是"刷新一次就把整个会话作废"）
    again = await anon_client.post("/api/auth/refresh", json={"refresh_token": new_refresh})
    assert again.status_code == 200


async def test_logout_invalidates_refresh(anon_client, accounts):
    created = await _register(anon_client, accounts, "logout")

    out = await anon_client.post(
        "/api/auth/logout", json={"refresh_token": created["refresh_token"]}
    )
    assert out.status_code == 200

    resp = await anon_client.post(
        "/api/auth/refresh", json={"refresh_token": created["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_logout_is_idempotent_for_garbage_token(anon_client):
    """
    登出**必须**幂等：token 已经烂了也回 200。

    用户点的是"退出"，服务端发生什么都应该表现为"退出成功"。
    这里回 401 的话，前端会卡在"登不出去"的状态里。
    """
    resp = await anon_client.post("/api/auth/logout", json={"refresh_token": "garbage"})
    assert resp.status_code == 200


# ============================================================
# 四、改密
# ============================================================


async def test_change_password_full_flow(anon_client, accounts):
    """
    改密的完整因果链：
      旧密码错 → 401
      新旧相同 → 400（这条容易被漏：允许"改成一样"等于白改一次）
      改成功后 → 旧密码登不进、新密码能进、**改密前的 refresh 全废**
    """
    created = await _register(anon_client, accounts, "chpwd")
    new_password = "BrandNewPass456"
    auth = _bearer(created["access_token"])

    wrong_old = await anon_client.post(
        "/api/auth/change-password",
        json={"old_password": "NotMyPassword1", "new_password": new_password},
        headers=auth,
    )
    assert wrong_old.status_code == 401

    same = await anon_client.post(
        "/api/auth/change-password",
        json={"old_password": created["_password"], "new_password": created["_password"]},
        headers=auth,
    )
    assert same.status_code == 400

    ok = await anon_client.post(
        "/api/auth/change-password",
        json={"old_password": created["_password"], "new_password": new_password},
        headers=auth,
    )
    assert ok.status_code == 200

    old_login = await anon_client.post(
        "/api/auth/login",
        json={"email": created["_email"], "password": created["_password"]},
    )
    assert old_login.status_code == 401

    new_login = await anon_client.post(
        "/api/auth/login", json={"email": created["_email"], "password": new_password}
    )
    assert new_login.status_code == 200

    # ★ 改密**之前**签发的 refresh：靠 password_changed_at 与 claims.iat 比对拦下
    stale = await anon_client.post(
        "/api/auth/refresh", json={"refresh_token": created["refresh_token"]}
    )
    assert stale.status_code == 401


async def test_change_password_requires_login(anon_client):
    resp = await anon_client.post(
        "/api/auth/change-password",
        json={"old_password": "a", "new_password": "BrandNewPass456"},
    )
    assert resp.status_code == 401


# ============================================================
# 五、找回密码
# ============================================================


async def test_forgot_password_same_response_for_unknown_email(anon_client):
    """
    ★ 邮箱不存在时，**返回体与存在时一致**（防枚举）。

    唯一允许的差别是 `dev_link`：它只在 "dev 环境 + 没配 SMTP" 时出现，
    而且存在与否取决于"邮件有没有发出去"，不是"邮箱在不在库里"。
    """
    resp = await anon_client.post(
        "/api/auth/forgot-password", json={"email": "nobody-here@agentforge.local"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "如果该邮箱已注册" in body["message"]
    assert body["dev_link"] is None


async def test_forgot_password_dev_link_then_reset(anon_client, accounts, monkeypatch):
    """
    本地无 SMTP 时的完整找回链路：申请 → 拿到 dev_link → 用令牌改密 → 旧密码失效。

    ⚠ 用 monkeypatch 把两个条件都**显式**钉住（app_env=dev、未配 SMTP），
      而不是依赖本机 .env —— 否则这条用例在 CI（APP_ENV=prod）上会一直红，
      而它测的"令牌能用"其实与部署环境无关。
    """
    from urllib.parse import parse_qs, urlparse

    from app.config import settings
    from app.services import mail_service

    created = await _register(anon_client, accounts, "reset")
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(mail_service, "is_configured", lambda: False)
    monkeypatch.setattr(mail_service, "send_mail", lambda **kw: False)

    resp = await anon_client.post(
        "/api/auth/forgot-password", json={"email": created["_email"]}
    )
    assert resp.status_code == 200
    link = resp.json()["dev_link"]
    assert link, "dev 环境下应把重置链接随响应返回，否则本地整条链路无法验收"

    token = parse_qs(urlparse(link).query)["token"][0]
    new_password = "ResetPass789"

    done = await anon_client.post(
        "/api/auth/reset-password", json={"token": token, "new_password": new_password}
    )
    assert done.status_code == 200

    # 旧密码失效、新密码可用
    assert (
        await anon_client.post(
            "/api/auth/login",
            json={"email": created["_email"], "password": created["_password"]},
        )
    ).status_code == 401
    assert (
        await anon_client.post(
            "/api/auth/login", json={"email": created["_email"], "password": new_password}
        )
    ).status_code == 200

    # ★ 一次性：同一个令牌不能再用第二次
    replay = await anon_client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "AnotherPass000"},
    )
    assert replay.status_code == 401


async def test_forgot_password_does_not_leak_link_in_prod(anon_client, accounts, monkeypatch):
    """
    ★ 生产环境**哪怕没配 SMTP** 也不许回 dev_link。

    这是把"配置事故"和"安全兜底"分开：邮件发不出去应该报警让人去修配置，
    而不是把重置令牌直接交给任何知道这个邮箱的人。
    """
    from app.config import settings
    from app.services import mail_service

    created = await _register(anon_client, accounts, "prod")
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(mail_service, "is_configured", lambda: False)
    monkeypatch.setattr(mail_service, "send_mail", lambda **kw: False)

    resp = await anon_client.post(
        "/api/auth/forgot-password", json={"email": created["_email"]}
    )
    assert resp.status_code == 200
    assert resp.json()["dev_link"] is None


async def test_reset_password_rejects_unknown_token(anon_client):
    resp = await anon_client.post(
        "/api/auth/reset-password",
        json={"token": "this-token-never-existed", "new_password": "WhateverPass1"},
    )
    assert resp.status_code == 401
