"""
请求级依赖：当前用户 / 管理员校验
==================================
FastAPI 的 `Depends` 是"在路由函数执行**之前**跑的一段代码"，它抛异常就等于
"这次请求到此为止"。鉴权正是它的典型用途：路由函数里拿到的 `user` 一定已经
验过签名、查过库、确认没被禁用 ——**路由里不需要再写任何 if**。

为什么单独一个模块（而不是塞进 `app/api/auth.py`）：
  除了 auth 路由，sessions / documents / tool_servers / models / eval / traces
  全都要用同一套依赖。放在 api 层会让 `api.chat` 反过来 import `api.auth` ——
  路由模块之间互相 import 是循环依赖的温床。`deps.py` 是"谁都可以依赖、它不依赖谁"。

三个刻意的决定：

1. **每个请求都回查一次 users 表**（不是只验签名）
   JWT 自带 role，理论上解出 claims 就够。但那样**禁用一个账号要等 2 小时才生效** ——
   管理员点了"禁用"，对方还能继续用，这是不能接受的。
   代价是每请求多一条主键查询（微秒级，走索引）。安全与一个 index lookup 之间，
   没有取舍余地。
   ⚠ 这也意味着 `security.py` 里"access token 不查 Redis"的说法只对 **Redis** 成立，
     对 PostgreSQL 不成立 —— 别把它理解成"鉴权零 IO"。

2. **`HTTPBearer(auto_error=False)`**
   默认 `auto_error=True` 会在缺 Authorization 头时返回 403 且文案是
   "Not authenticated"（其实是 401 的语义）。关掉它，自己决定状态码与文案，
   顺便让"没有凭证"和"凭证无效"回同一句话 —— 少给攻击者一个信号。

3. **`require_admin` 只认库里的 role，不认 token 里的 role**
   token 里的 role 是**签发那一刻的快照**。管理员被降级后，他手上那张未过期的
   access token 仍写着 `role=admin`。若按 claims 判权，降级要等 2 小时才生效 ——
   与第 1 条同一个理由。所以 `get_current_user` 返回的是**库里刚读出来的行**，
   `require_admin` 直接看 `user.role`。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.logging import logger
from app.core.security import TOKEN_TYPE_ACCESS, TokenError, decode_token, token_user_id
from app.models.user import ROLE_ADMIN, User
from app.services import auth_service

# auto_error=False：缺头时给 None 而不是抛 403，见文件头第 2 条
_bearer = HTTPBearer(auto_error=False, description="Authorization: Bearer <access_token>")

# 401 的文案**必须统一**：不区分"没带 token""token 过期""用户不存在""账号被禁用"。
# 前端只按状态码分支（401 → 跳登录），文案给人看时不需要更细。
_UNAUTHORIZED = "登录状态无效，请重新登录"


def _unauthorized(detail: str = _UNAUTHORIZED) -> HTTPException:
    # WWW-Authenticate 是 RFC 6750 要求的响应头，缺了某些客户端不会触发重新认证
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """
    解出当前用户。**所有需要登录的接口都依赖它**。

    Raises:
        HTTPException 401: 没带凭证 / 签名不对 / 已过期 / 类型不是 access /
                           用户不存在 / 账号已禁用
    """
    if cred is None or not cred.credentials:
        raise _unauthorized("请先登录")

    try:
        # expected_type 必须是 access：否则一个 7 天有效的 refresh token
        # 就能当接口凭证用（这是最容易被忽略的越权口子）
        payload = decode_token(cred.credentials, expected_type=TOKEN_TYPE_ACCESS)
        user_id = token_user_id(payload)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    user = await auth_service.get_user(user_id)
    if user is None:
        # 用户被删了但 token 还没过期 —— 常见于"重建数据库"后拿旧 token 访问
        raise _unauthorized()
    if not user.is_active:
        logger.warning("已禁用账号尝试访问 user=%s", user.email)
        raise _unauthorized()
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """管理员专用接口的依赖。非 admin 一律 403（**不是** 401 —— 他登录是有效的）。"""
    if user.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


# ---- 类型别名：路由签名里写 `user: CurrentUser` 即可 ----
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


def owner_id_of(user: User) -> uuid.UUID:
    """
    取用户的归属 id（多租户过滤的锚点）。

    单独一个函数而不是到处写 `user.id`：将来若引入"团队空间"，
    归属的锚点会从 user.id 换成 team_id，那时只改这一处。
    """
    return user.id
