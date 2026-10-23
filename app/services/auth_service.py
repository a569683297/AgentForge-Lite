"""
认证服务：注册 / 登录 / 刷新 / 登出 / 改密 / 找回
================================================
所有账号状态的变更都在这里发生。路由层只做"参数校验 + 调这里 + 翻状态码"，
与 `session_service` / `document_service` 同一个分层原则。

四个安全上的硬约束（每条都对应一个真实的攻击面）：

1. **登录失败不区分"用户不存在"和"密码错误"**
   区分了就等于提供用户名枚举接口 —— 攻击者先批量确认哪些邮箱注册过，
   再针对这些账号撞库。所以两种情况返回同一句话、同一个状态码。

2. **用户不存在时也要跑一次 bcrypt**
   否则"不存在"立刻返回、"存在但密码错"要 250ms —— 响应时间本身就是答案。
   这里用一个预置的假哈希跑满一次 `checkpw`，把两条路径的耗时拉平。

3. **找回密码永远回同一句话**
   "如果该邮箱已注册，我们已发送重置链接"。区分了同样是枚举接口。

4. **refresh token 轮换（rotation）+ 白名单**
   JWT 无法吊销，所以 refresh 的 jti 必须落 Redis；刷新时删旧签新。
   这样"登出即失效"成立，且旧 token 被重放时会立刻失败（因为它已不在白名单里）。

关于 `password_changed_at`：
  改密后，**改密之前签发的 refresh token 全部失效**（比对 claims.iat）。
  已签发的 access token 仍有效到过期（最长 2 小时）—— 这是无状态 JWT 的固有代价，
  必须写进文档，不能假装"改密立刻踢下线"。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.core.db import async_session_factory
from app.core.logging import logger
from app.core.redis import redis_client
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    TokenError,
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
    token_user_id,
    verify_password,
)
from app.models.audit_log import (
    ACTION_LOGIN,
    ACTION_LOGIN_FAILED,
    ACTION_PASSWORD_CHANGED,
    ACTION_PASSWORD_RESET,
    ACTION_REGISTER,
)
from app.models.user import ROLE_ADMIN, ROLE_USER, User
from app.schemas.auth import UserOut
from app.services import audit_service

# ---- Redis 键（唯一出处）----
_REFRESH_KEY = "auth:refresh:{jti}"      # refresh 白名单：jti → user_id
_RESET_KEY = "auth:reset:{digest}"       # 找回密码令牌：sha256(token) → user_id
_LOGIN_FAIL_KEY = "auth:fail:{email}"    # 登录失败计数（限流用）

# 登录失败限流：同一邮箱连续失败 5 次，锁 5 分钟。
# 为什么按邮箱而不是按 IP：本项目没有网关层，IP 可能是反向代理的地址（全是 127.0.0.1）。
# 代价是"攻击者可以锁死别人的账号"—— 这是已知取舍，比"不限流"好。
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 300

# 用户不存在时用来"陪跑"的假哈希（成本因子 12，与生产真实哈希等价，实测 166ms/次）。
# ⚠ 它必须是**格式合法、成本因子正确**的真实 bcrypt 串。
#   如果随便手写一个假串，bcrypt 会在解析阶段就报错并立刻返回 ——
#   那反而把"用户不存在"变成 1ms、"密码错"变成 166ms，比不陪跑还糟。
# 这个串由一个随机 32 字节密码生成（随机串未记录），任何输入都不可能匹配。
# `tests/unit/test_security.py` 里有一条用例专门守这个不变量。
_DUMMY_HASH = "$2b$12$4xjuRglf2rrm7W5ZL9cO.u3VbIHz.AyuO2d2iP7DfyZNahMZrI/RW"


class AuthError(Exception):
    """认证类错误的基类。路由层按子类翻译成 4xx。"""


class EmailTaken(AuthError):
    """邮箱已注册（409）。"""


class InvalidCredentials(AuthError):
    """邮箱或密码错误 / 账号已禁用（401，**同一条文案**）。"""


class AuthUnavailable(AuthError):
    """Redis 不可用导致无法完成认证（503）。不降级放行 —— 见下方说明。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# Redis 访问（认证路径上**不吞异常**）
# ============================================================
# 与 memory_service 的取舍正好相反：那里 Redis 挂了要降级（对话还能继续），
# 这里挂了必须**失败**（fail closed）。
# 理由：记忆丢了只是"忘了上文"，而认证的降级等于"谁都能进"。
# 一个读不到白名单的 refresh token，既可能是 Redis 抖动，也可能是有人在刷 ——
# 这两种情况在服务端长得一模一样，唯一安全的处理是都不放行。


async def _redis_set(key: str, value: str, ttl: int) -> None:
    try:
        await redis_client.set(key, value, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.error("认证 Redis 写入失败 key=%s err=%s", key, exc)
        raise AuthUnavailable("认证服务暂不可用，请稍后重试") from exc


async def _redis_get(key: str) -> str | None:
    try:
        return await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.error("认证 Redis 读取失败 key=%s err=%s", key, exc)
        raise AuthUnavailable("认证服务暂不可用，请稍后重试") from exc


async def _redis_delete(key: str) -> None:
    try:
        await redis_client.delete(key)
    except Exception as exc:  # noqa: BLE001
        # 删不掉只会让 token 多活一会儿（到 TTL），不该阻断登出
        logger.warning("认证 Redis 删除失败 key=%s err=%s", key, exc)


# ============================================================
# 查询
# ============================================================


async def get_user(user_id: uuid.UUID) -> User | None:
    async with async_session_factory() as session:
        return await session.get(User, user_id)


async def get_user_by_email(email: str) -> User | None:
    async with async_session_factory() as session:
        return (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()


async def count_users() -> int:
    """引导判断用：库里一个用户都没有时，第一个注册的人自动成为 admin。"""
    from sqlalchemy import func

    async with async_session_factory() as session:
        return (await session.execute(select(func.count()).select_from(User))).scalar_one()


# ============================================================
# 注册 / 登录
# ============================================================


async def register(
    *, email: str, password: str, display_name: str = ""
) -> User:
    """
    注册。

    「第一个用户是 admin」这条规则写在这里而不是路由层：
      它是**业务规则**（空库时没有管理员，谁来做管理操作？），
      换一个入口（CLI、迁移脚本）也应该成立。
    并发下可能有两个人都撞上"库里只有 0 个"—— 那时两人都是 admin，
    但这只发生在"全新库的第一次注册"，实际风险为零；用唯一索引挡邮箱就够了。

    Raises:
        EmailTaken: 邮箱已存在（409）
        ValueError: 密码不合规（400，由 security.hash_password 抛）
    """
    existing = await get_user_by_email(email)
    if existing is not None:
        raise EmailTaken("该邮箱已注册")

    password_hash = hash_password(password)   # 可能抛 ValueError（超长）
    is_first = await count_users() == 0

    user = User(
        email=email,
        password_hash=password_hash,
        display_name=display_name or email.split("@")[0],
        role=ROLE_ADMIN if is_first else ROLE_USER,
        is_active=True,
    )
    async with async_session_factory() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)

    logger.info(
        "用户注册成功 email=%s role=%s（库里首个用户？%s）", user.email, user.role, is_first
    )
    await audit_service.record(
        action=ACTION_REGISTER,
        actor_id=user.id,
        actor_email=user.email,
        target_type="user",
        target_id=user.id,
        detail={"role": user.role, "is_first_user": is_first},
    )
    return user


async def authenticate(*, email: str, password: str) -> User:
    """
    校验邮箱密码，返回用户。

    Raises:
        InvalidCredentials: **任何**失败原因（不存在 / 密码错 / 已禁用）
        AuthUnavailable: Redis 不可用（限流计数读写失败）
    """
    # ① 限流：先看这个邮箱是不是正在锁定期
    fail_key = _LOGIN_FAIL_KEY.format(email=email)
    failures = await _redis_get(fail_key)
    if failures is not None and int(failures) >= MAX_LOGIN_FAILURES:
        # 文案与"密码错"一致，不告诉攻击者"你被锁了"还是"密码不对"
        logger.warning("登录被限流 email=%s failures=%s", email, failures)
        raise InvalidCredentials("邮箱或密码错误")

    user = await get_user_by_email(email)

    # ② 陪跑：用户不存在时也做一次 bcrypt，把耗时拉平（防时序侧信道）
    if user is None:
        verify_password(password, _DUMMY_HASH)
        await _bump_failures(fail_key)
        await _audit_login_failed(email, reason="user_not_found")
        raise InvalidCredentials("邮箱或密码错误")

    if not verify_password(password, user.password_hash):
        await _bump_failures(fail_key)
        await _audit_login_failed(email, reason="bad_password", user_id=user.id)
        raise InvalidCredentials("邮箱或密码错误")

    if not user.is_active:
        # 禁用账号：密码对也不让进。文案仍与密码错一致，
        # 否则"这个号存在但被禁了"也是一条信息。
        await _audit_login_failed(email, reason="inactive", user_id=user.id)
        raise InvalidCredentials("邮箱或密码错误")

    # ③ 成功：清限流计数 + 记录登录时间（hash 强度不足时顺手升级）
    await _redis_delete(fail_key)
    async with async_session_factory() as session:
        fresh = await session.get(User, user.id)
        if fresh is not None:
            fresh.last_login_at = _now()
            if needs_rehash(fresh.password_hash):
                fresh.password_hash = hash_password(password)
                logger.info("登录时升级密码哈希强度 user=%s", fresh.email)
            await session.commit()
            await session.refresh(fresh)
            user = fresh

    await audit_service.record(
        action=ACTION_LOGIN,
        actor_id=user.id,
        actor_email=user.email,
        target_type="user",
        target_id=user.id,
    )
    return user


async def _audit_login_failed(
    email: str, *, reason: str, user_id: uuid.UUID | None = None
) -> None:
    """
    记录一次登录失败。

    ⚠ 审计里**必须**区分失败原因（user_not_found / bad_password / inactive）——
      这与"对外文案不能区分"并不矛盾：
      对外不区分是防**枚举**（攻击者靠响应判断账号存在性）；
      对内必须区分是给**运维**看（"一堆 bad_password"是撞库，
      "一堆 user_not_found"是有人在扫邮箱，"一堆 inactive"是有人用已禁用的号在试）。
      把这两件事混为一谈，最后的结果通常是两边都做不好。
    """
    await audit_service.record(
        action=ACTION_LOGIN_FAILED,
        actor_id=user_id,
        actor_email=email,
        target_type="user",
        target_id=user_id or "",
        detail={"reason": reason},
    )


async def _bump_failures(key: str) -> None:
    """失败计数 +1，TTL 只在第一次设置时给（否则每次失败都重置窗口）。"""
    try:
        value = await redis_client.incr(key)
        if value == 1:
            await redis_client.expire(key, LOGIN_LOCK_SECONDS)
    except Exception as exc:  # noqa: BLE001
        # 限流不可用时**不阻断登录**：它只是防护，不是正确性。
        # （与上面"白名单必须 fail closed"不同 —— 那个关系到授权，这个只关系到防爆破。）
        logger.warning("登录失败计数写入失败（不影响本次判定）err=%s", exc)


# ============================================================
# 令牌
# ============================================================


async def issue_tokens(user: User) -> dict:
    """
    签发一对 token，并把 refresh 的 jti 写进白名单。

    Returns:
        可直接塞进 `TokenPairOut` 的 dict（含 user）。

    ⚠ `user` 这里是 `UserOut.from_user(...)` 而不是 ORM 对象：
      响应里必须带上 `permissions`（前端据此决定显示哪些入口）。
      交给 Pydantic 从 ORM 自动转换的话，permissions 只会落到默认空值 ——
      表现为"登录后菜单全没了"，而且后端一切正常、日志无异常。
    """
    access, _, access_exp = create_token(
        user.id, token_type=TOKEN_TYPE_ACCESS, role=user.role
    )
    refresh, refresh_jti, _ = create_token(
        user.id, token_type=TOKEN_TYPE_REFRESH, role=user.role
    )
    await _redis_set(
        _REFRESH_KEY.format(jti=refresh_jti),
        str(user.id),
        ttl=settings.auth_refresh_ttl_days * 86400,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": int((access_exp - _now()).total_seconds()),
        "user": UserOut.from_user(user),
    }


async def refresh_tokens(refresh_token: str) -> dict:
    """
    用 refresh 换一对新 token（**轮换**：旧的那份立刻作废）。

    Raises:
        InvalidCredentials: 令牌无效 / 已登出 / 改密前签发 / 账号已禁用
    """
    try:
        payload = decode_token(refresh_token, expected_type=TOKEN_TYPE_REFRESH)
        user_id = token_user_id(payload)
    except TokenError as exc:
        raise InvalidCredentials(str(exc)) from exc

    jti = str(payload.get("jti") or "")
    key = _REFRESH_KEY.format(jti=jti)
    owner = await _redis_get(key)
    if owner is None or owner != str(user_id):
        # 不在白名单 = 已登出 / 已被轮换 / 伪造。三种情况一律拒绝。
        raise InvalidCredentials("登录已失效，请重新登录")

    user = await get_user(user_id)
    if user is None or not user.is_active:
        await _redis_delete(key)
        raise InvalidCredentials("登录已失效，请重新登录")

    # 改密之前签发的 refresh 一律作废（见文件头关于 password_changed_at 的说明）
    changed = user.password_changed_at
    if changed is not None:
        issued = payload.get("iat")
        if isinstance(issued, (int, float)) and issued < changed.timestamp():
            await _redis_delete(key)
            raise InvalidCredentials("密码已变更，请重新登录")

    # 轮换：先删旧的，再签新的。顺序不能反 ——
    # 反过来的话，若签新失败，旧 token 还在，用户手里会有两份有效凭证。
    await _redis_delete(key)
    return await issue_tokens(user)


async def logout(refresh_token: str) -> None:
    """
    登出：把 refresh 的 jti 从白名单里删掉。

    ⚠ 已经签发的 access token **仍然有效到过期**（最多 2 小时）。
      这是无状态 JWT 的代价，不是 bug。要"立刻踢下线"只能：
      ① 把 access TTL 缩到几分钟 ② 维护 access 黑名单（每个请求查一次 Redis）
      本项目选 ①+短 TTL 的组合思路（2 小时），没有做 ②。

    解析失败时静默返回：登出接口不该因为"token 已经烂了"而报错 ——
    用户点的就是"退出"，结果无论如何都是"退出了"。
    """
    try:
        payload = decode_token(refresh_token, expected_type=TOKEN_TYPE_REFRESH)
    except TokenError:
        return
    await _redis_delete(_REFRESH_KEY.format(jti=str(payload.get("jti") or "")))


# ============================================================
# 改密 / 找回
# ============================================================


async def change_password(
    *, user: User, old_password: str, new_password: str
) -> None:
    """
    改密。成功后会作废所有"改密前签发的" refresh token（access 除外）。

    Raises:
        InvalidCredentials: 旧密码错（401）
        ValueError: 新密码不合规（400）
    """
    if not verify_password(old_password, user.password_hash):
        raise InvalidCredentials("原密码不正确")
    if old_password == new_password:
        raise ValueError("新密码不能与原密码相同")

    new_hash = hash_password(new_password)
    async with async_session_factory() as session:
        fresh = await session.get(User, user.id)
        if fresh is None:
            raise InvalidCredentials("用户不存在")
        fresh.password_hash = new_hash
        fresh.password_changed_at = _now()
        await session.commit()

    logger.info("密码已变更 user=%s", user.email)
    await audit_service.record(
        action=ACTION_PASSWORD_CHANGED,
        actor_id=user.id,
        actor_email=user.email,
        target_type="user",
        target_id=user.id,
    )


def _reset_digest(token: str) -> str:
    """
    重置令牌在 Redis 里**只存摘要**，不存原文。

    理由与密码相同：Redis 快照/备份泄露时，拿到摘要也换不了密码。
    用 sha256 而不是 bcrypt：这里的令牌是 32 字节高熵随机串，
    不存在"字典攻击"的问题，用慢哈希只会让每次重置多花 250ms。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_reset_token(email: str) -> tuple[str | None, str | None]:
    """
    生成一次性重置令牌。

    Returns:
        (token, path)。邮箱不存在时返回 (None, None) ——
        **调用方必须无视这个区别，对外永远回同一句话**（防枚举）。
        返回的是**相对路径**（`/reset-password?token=...`），绝对地址由接口层
        用 `frontend_base_url` 拼 —— service 层不该知道部署在哪个域名下。

    令牌形态：`secrets.token_urlsafe(32)`（43 字符，256 bit 熵，不可猜）。
    Redis 里存 sha256(令牌) → user_id，TTL 30 分钟，**用一次即删**。
    """
    user = await get_user_by_email(email)
    if user is None:
        return None, None

    token = secrets.token_urlsafe(32)
    await _redis_set(
        _RESET_KEY.format(digest=_reset_digest(token)),
        str(user.id),
        ttl=settings.auth_reset_ttl_min * 60,
    )
    # 把路径拼出来放在这里（而不是让调用方拼）：路由层不该知道前端路由长什么样
    return token, f"/reset-password?token={token}"


async def reset_password(*, token: str, new_password: str) -> None:
    """
    用一次性令牌重置密码。

    Raises:
        InvalidCredentials: 令牌无效 / 已用过 / 已过期
        ValueError: 新密码不合规
    """
    key = _RESET_KEY.format(digest=_reset_digest(token))
    owner = await _redis_get(key)
    if owner is None:
        raise InvalidCredentials("重置链接无效或已过期，请重新申请")

    new_hash = hash_password(new_password)
    async with async_session_factory() as session:
        user = await session.get(User, uuid.UUID(owner))
        if user is None:
            await _redis_delete(key)
            raise InvalidCredentials("重置链接无效或已过期，请重新申请")
        user.password_hash = new_hash
        user.password_changed_at = _now()
        await session.commit()
        email = user.email

    # 先改密、再删令牌：反过来的话，改密失败时令牌已经没了，用户得重新申请
    await _redis_delete(key)
    logger.info("密码已通过重置令牌变更 user=%s", email)
    await audit_service.record(
        action=ACTION_PASSWORD_RESET,
        actor_email=email,
        target_type="user",
        detail={"via": "reset_token"},
    )


# ============================================================
# 管理员
# ============================================================


async def list_users(*, limit: int = 100, offset: int = 0) -> list[User]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
                )
            ).scalars()
        )


async def set_active(*, user_id: uuid.UUID, active: bool) -> User:
    """启用/禁用账号。禁用后：登录被拒、已签发的 refresh 也刷不出新 token。"""
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise LookupError("用户不存在")
        user.is_active = active
        await session.commit()
        await session.refresh(user)
    logger.info("账号状态变更 user=%s active=%s", user.email, active)
    return user


async def set_role(*, user_id: uuid.UUID, role: str) -> User:
    if role not in (ROLE_USER, ROLE_ADMIN):
        raise ValueError(f"未知角色：{role!r}")
    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise LookupError("用户不存在")
        user.role = role
        await session.commit()
        await session.refresh(user)
    logger.info("账号角色变更 user=%s role=%s", user.email, role)
    return user


__all__ = [
    "AuthError",
    "EmailTaken",
    "InvalidCredentials",
    "AuthUnavailable",
    "register",
    "authenticate",
    "issue_tokens",
    "refresh_tokens",
    "logout",
    "change_password",
    "create_reset_token",
    "reset_password",
    "get_user",
    "get_user_by_email",
    "list_users",
    "set_active",
    "set_role",
]
