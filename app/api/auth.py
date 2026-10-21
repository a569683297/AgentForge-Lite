"""
账号接口
========
九个出口，全部在 `/api/auth` 下：

    POST   /api/auth/register          注册（成功即登录，返回 token 对）      201
    POST   /api/auth/login             登录                                    200
    POST   /api/auth/refresh           用 refresh 换新 token 对（轮换）        200
    POST   /api/auth/logout            登出（作废 refresh）                    200
    GET    /api/auth/me                当前用户                                200
    POST   /api/auth/change-password   改密（需登录）                          200
    POST   /api/auth/forgot-password   申请重置链接（**永远**回同一句话）      200
    POST   /api/auth/reset-password    用一次性令牌重置密码                    200

分层：本文件只做「收参数 → 调 service → 翻状态码」。
没有一行密码学代码，没有一个 if 判断"密码对不对" —— 那些都在
`app/core/security.py` 与 `app/services/auth_service.py`。

为什么"忘记密码"不能返回 404：
  一旦"邮箱不存在"回 404、"存在"回 200，这个接口就成了**批量验证邮箱是否注册**
  的工具。所以它永远返回 200 + 同一句话；真正没发邮件这件事只体现在
  "用户收不到信"上。

为什么注册成功直接发 token（不要求再登录一次）：
  注册表单刚让用户输了密码，紧接着让他再输一遍是纯粹的摩擦。
  安全性上没有区别 —— 能拿到注册响应的人本来就掌握了明文密码。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.core.logging import logger
from app.deps import CurrentUser
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordOut,
    ForgotPasswordRequest,
    LoginRequest,
    MessageOut,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenPairOut,
    UserOut,
)
from app.services import auth_service, mail_service
from app.services.auth_service import (
    AuthUnavailable,
    EmailTaken,
    InvalidCredentials,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ------------------------------------------------------------
# 异常 → 状态码（统一在这里翻译，service 层只抛领域异常）
# ------------------------------------------------------------
# 为什么不用 app 级 exception_handler：
#   那样所有接口都会吃到这套映射，而 auth 的语义只属于 auth。
#   更重要的是：**本文件里每一个 except 都是显式的** —— 读代码时能一眼看出
#   "这个失败会变成什么状态码"，不用去 main.py 里翻注册表。


def _http_from(exc: auth_service.AuthError) -> HTTPException:
    if isinstance(exc, EmailTaken):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, InvalidCredentials):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )
    if isinstance(exc, AuthUnavailable):
        # 503 而不是 500：这是"暂时不可用，稍后再试"，不是"服务有 bug"
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _bad_request(exc: ValueError) -> HTTPException:
    """密码不合规（太短 / 太长 / 与原密码相同）—— 都是 400。"""
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ============================================================
# 注册 / 登录
# ============================================================


@router.post(
    "/register",
    response_model=TokenPairOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册（成功即登录）",
)
async def register_api(payload: RegisterRequest) -> dict:
    """
    注册并签发 token。

    ⚠ 第一个注册的人是 admin（空库引导）。这条规则在 service 层，
      所以 CLI / 迁移脚本走同一个入口时行为一致。
      若你不想让"第一个注册的人"拿到管理员，就用 `AUTH_BOOTSTRAP_*` 预置账号。
    """
    try:
        user = await auth_service.register(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
    except (EmailTaken, AuthUnavailable) as exc:
        raise _http_from(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc

    try:
        return await auth_service.issue_tokens(user)
    except AuthUnavailable as exc:
        # 用户建好了但 Redis 挂了 → 发不出 refresh。
        # 这时**不能**回滚用户（已经提交），只能告诉前端"稍后用登录接口"。
        raise _http_from(exc) from exc


@router.post("/login", response_model=TokenPairOut, summary="登录")
async def login_api(payload: LoginRequest) -> dict:
    """
    登录。

    失败（用户不存在 / 密码错 / 已禁用）**一律** 401 + "邮箱或密码错误"。
    连续失败 5 次会锁 5 分钟，但仍然回同一句话 —— 不告诉攻击者"你被锁了"。
    """
    try:
        user = await auth_service.authenticate(
            email=payload.email, password=payload.password
        )
        return await auth_service.issue_tokens(user)
    except (InvalidCredentials, AuthUnavailable) as exc:
        raise _http_from(exc) from exc


@router.post("/refresh", response_model=TokenPairOut, summary="刷新令牌（轮换）")
async def refresh_api(payload: RefreshRequest) -> dict:
    """
    用 refresh token 换一对新 token。

    ⚠ **轮换**：成功响应里的 `refresh_token` 与请求里的**不是同一个**，
      前端必须把它覆盖掉。仍拿旧的再刷一次会 401（旧 jti 已从白名单删除）。
      这是"refresh token 被盗后只能被用一次"的实现方式。
    """
    try:
        return await auth_service.refresh_tokens(payload.refresh_token)
    except (InvalidCredentials, AuthUnavailable) as exc:
        raise _http_from(exc) from exc


@router.post("/logout", response_model=MessageOut, summary="登出")
async def logout_api(payload: RefreshRequest) -> dict:
    """
    登出。只需 refresh token（它才是可吊销的那一份）。

    幂等：token 已经烂了/过期了也回 200 —— 用户点的就是"退出"，
    无论服务端发生什么都应该表现为"退出成功"。
    """
    await auth_service.logout(payload.refresh_token)
    return {"message": "已退出登录"}


# ============================================================
# 当前用户 / 改密
# ============================================================


@router.get("/me", response_model=UserOut, summary="当前用户")
async def me_api(user: CurrentUser) -> UserOut:
    """
    前端启动时用它校验本地 token 是否还有效（顺带拿到最新 role）。

    `user` 是 `deps.get_current_user` 的产物 —— 已经验过签名、
    查过库、确认没过期没被禁用。
    """
    return UserOut.model_validate(user)


@router.post("/change-password", response_model=MessageOut, summary="修改密码")
async def change_password_api(payload: ChangePasswordRequest, user: CurrentUser) -> dict:
    """
    改密。

    ⚠ 改密后：**改密前签发的 refresh token 全部失效**（比对 `password_changed_at`
      与 token 的 `iat`），但已签发的 access token 仍有效到过期（最长 2 小时）。
      原因是 access 是无状态 JWT，服务端不查表就无法吊销。
      前端收到这个响应后应主动清本地凭证并跳登录 —— 但**别指望这是强制的**。
    """
    try:
        await auth_service.change_password(
            user=user,
            old_password=payload.old_password,
            new_password=payload.new_password,
        )
    except InvalidCredentials as exc:
        raise _http_from(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"message": "密码已修改，请重新登录"}


# ============================================================
# 找回密码
# ============================================================


def _abs_link(path: str) -> str:
    """
    相对路径 → 绝对地址。

    用 `rstrip("/")` 拼而不是 f"{base}{path}"：`FRONTEND_BASE_URL` 常被写成
    `http://localhost:8080/`（带尾斜杠），直拼会得到 `//reset-password`，
    浏览器能容错，但日志和邮件里很难看，也比较难排查。
    """
    return f"{settings.frontend_base_url.rstrip('/')}{path}"


def _dev_link_allowed() -> bool:
    """
    是否允许在响应里回 `dev_link`。

    两个条件同时成立才行：**开发环境** + **没配 SMTP**。
    少任何一个都可能把重置令牌直接送给任何知道邮箱的人。
    （生产环境哪怕 SMTP 没配也不许回 —— 那是配置事故，不该用泄露令牌来兜底。）
    """
    return settings.app_env == "dev" and not mail_service.is_configured()


@router.post("/forgot-password", response_model=ForgotPasswordOut, summary="申请重置链接")
async def forgot_password_api(payload: ForgotPasswordRequest) -> dict:
    """
    申请重置密码的邮件。

    **无论邮箱是否存在，返回体完全一致**（除了 dev_link 这个本地调试字段，
    它只在 dev + 未配 SMTP 时出现，生产恒为 null）。
    """
    message = "如果该邮箱已注册，重置链接已发送，请查收（含垃圾邮件箱）"

    try:
        token, path = await auth_service.create_reset_token(payload.email)
    except AuthUnavailable as exc:
        # Redis 挂了确实发不出令牌。这时**仍然回同一句话**：
        # 否则"Redis 挂了"与"邮箱没注册"又变成可区分的两种状态了。
        logger.error("找回密码失败（Redis 不可用）：%s", exc)
        return {"message": message, "dev_link": None}

    dev_link: str | None = None
    if token and path:
        link = _abs_link(path)
        body = mail_service.build_reset_body(link, ttl_min=settings.auth_reset_ttl_min)
        sent = mail_service.send_mail(
            to=payload.email, subject="重置你的 AgentForge 密码", body=body
        )
        if not sent and _dev_link_allowed():
            # 本地没配 SMTP：把链接回给前端，让整条链路能验收
            dev_link = link
            logger.info("找回密码（dev 模式，链接随响应返回）email=%s", payload.email)

    return {"message": message, "dev_link": dev_link}


@router.post("/reset-password", response_model=MessageOut, summary="用令牌重置密码")
async def reset_password_api(payload: ResetPasswordRequest) -> dict:
    """
    用邮件里的一次性令牌设新密码。

    令牌**用一次即失效**（Redis 键在读到时不会立刻删，但改密成功后立即删）。
    """
    try:
        await auth_service.reset_password(
            token=payload.token, new_password=payload.new_password
        )
    except InvalidCredentials as exc:
        raise _http_from(exc) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"message": "密码已重置，请用新密码登录"}
