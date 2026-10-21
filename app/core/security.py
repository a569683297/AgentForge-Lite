"""
认证原语：密码哈希 + JWT 签发/校验
==================================
这是整个账号体系**唯一**做密码学运算的地方。其它模块（service / api / 前端）
都不许自己拼 token、更不许自己存明文密码 —— 与 `llm_gateway` 是"LLM 调用的唯一出口"
同一个原则：**安全相关的实现只能有一份**，否则某天有人写了个"简化版"就全线失守。

三个刻意的决定：

1. **bcrypt，成本因子 12**
   本机实测（arm64 / bcrypt 5.0.0）：4→0.7ms、10→42ms、**12→166ms**、14→663ms。
   它存在的意义就是"慢" —— 攻击者拿到库也只能每秒试 6 次，而不是每秒几百万次。
   代价是每次登录多 166ms，这是**必须付**的成本（登录不是高频路径）。
   ⚠ 不要在测试里用 12：251 条测试里但凡碰密码就会多出几百毫秒。测试用 4（快 230 倍），
   生产用 12 —— 成本因子写在哈希串里，验签时会自动读，两边的哈希可以互相校验。

2. **bcrypt 的 72 字节上限显式拒绝，不静默截断**
   bcrypt 只处理前 72 字节。老库遇到超长密码会**静默截断** —— 那意味着
   "密码前 72 字节相同即视为同一个密码"，而且用户完全不知道。
   这里直接抛错，让调用方在注册/改密时就告诉用户"太长了"。
   72 字节 ≈ 24 个汉字，够用。

3. **refresh token 用 Redis 白名单（可吊销），access token 不查库**
   JWT 本身无法吊销 —— 签出去就有效到过期。所以：
     · access（2 小时）：不查 Redis，纯签名校验。图的是"每请求零 IO"；
       短有效期是它的安全边界。
     · refresh（7 天）：jti 存 Redis，刷新时**轮换**（删旧签新）。
       这样"登出即失效""token 被盗用后旧的那份作废"都能成立。
   两者的取舍不同，因为它们的生命周期差 84 倍。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.config import settings

# ---- 常量（唯一出处）----
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
TokenType = Literal["access", "refresh"]

ALGORITHM = "HS256"

# bcrypt 的硬上限（字节）。不是我们的选择，是算法本身决定的。
BCRYPT_MAX_BYTES = 72

# 生产用的成本因子。12 → 实测 166ms/次（arm64 + bcrypt 5.0.0）。
# 为什么不是 10（42ms，快 4 倍）：10 在 2026 年的显卡面前已经偏弱；
# 为什么不是 14（663ms）：登录要 0.7 秒以上，用户会以为卡住了。
BCRYPT_ROUNDS = 12


class TokenError(Exception):
    """token 无效 / 过期 / 类型不符。上层据此返回 401，而不是 500。"""


_EPHEMERAL_SECRET: str = ""
"""进程内临时密钥的缓存。初始为空串 —— 与"未生成"是同一个状态。"""


def _secret() -> str:
    """
    取 JWT 签名密钥。

    配置为空时退回一个**进程内随机值**：开发期不配 .env 也能跑，
    但代价是进程重启后所有 token 失效（表现为"突然要重新登录"）。
    这个行为必须能被看出来 —— 所以启动日志里会警告一次（见 `warn_if_insecure`）。

    ⚠ 这里踩过一次坑：初版写的是 `try: return _EPHEMERAL_SECRET except NameError:` ——
      但模块级已经 `_EPHEMERAL_SECRET = ""`，那个 `try` **永远成功**，返回空串。
      pyjwt 对空密钥直接抛 `InvalidKeyError: HMAC key must not be empty`，
      于是"不配密钥也能跑"的兜底根本没生效。教训：**用「值是否为空」判断，
      而不是用「名字是否存在」判断** —— 后者只对未定义变量有效，对空值无效。
    """
    if settings.auth_secret_key:
        return settings.auth_secret_key
    global _EPHEMERAL_SECRET
    if not _EPHEMERAL_SECRET:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
    return _EPHEMERAL_SECRET


def warn_if_insecure() -> None:
    """
    启动自检：密钥没配就明确警告一次。

    为什么值得单写一个函数：`auth_secret_key` 留空时服务**照样能跑**，
    功能全对 —— 只在重启后集体掉登录。这种"没有症状的配置缺失"最容易被带到部署环境。
    """
    from app.core.logging import logger

    if not settings.auth_secret_key:
        logger.warning(
            "auth_secret_key 未配置：已生成进程内临时密钥，**服务重启后所有登录态失效**。"
            "部署前必须在 .env 里设置 AUTH_SECRET_KEY（可用 `openssl rand -base64 48` 生成）"
        )


# ============================================================
# 密码
# ============================================================


def hash_password(plain: str, *, rounds: int | None = None) -> str:
    """
    密码 → bcrypt 哈希串（自带盐与成本因子，形如 `$2b$12$...`）。

    Args:
        rounds: 成本因子。默认用生产的 12；**测试可传 4**（快 ~60 倍）。
    Raises:
        ValueError: 密码为空，或 UTF-8 编码后超过 72 字节
    """
    if not plain:
        raise ValueError("密码不能为空")

    raw = plain.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        # 显式拒绝而不是截断 —— 见文件头第 2 条
        raise ValueError(
            f"密码过长：UTF-8 编码后 {len(raw)} 字节，上限 {BCRYPT_MAX_BYTES} 字节"
            f"（约 {BCRYPT_MAX_BYTES // 3} 个汉字或 {BCRYPT_MAX_BYTES} 个 ASCII 字符）"
        )

    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS if rounds is None else rounds)
    return bcrypt.hashpw(raw, salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """
    校验密码。

    ⚠ 任何异常都返回 False，**不向上抛**：
      哈希串损坏（手工改库改坏了）、算法前缀不认识、编码异常 ——
      对调用方来说都是"这次验证没通过"，而不是"服务坏了"。
      抛出去会让一次脏数据变成 500，还会把"密码错误"和"系统故障"混在一起。
    """
    if not plain or not hashed:
        return False
    try:
        raw = plain.encode("utf-8")
        # 超长密码直接判失败（而不是截断后比较）—— 否则会与"注册时的拒绝"不一致
        if len(raw) > BCRYPT_MAX_BYTES:
            return False
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def needs_rehash(hashed: str) -> bool:
    """
    这个哈希串是不是用了低于当前标准的成本因子（登录成功时顺手升级）。

    典型场景：早期用 rounds=10 注册的用户，在某次登录后自动升级到 12 ——
    用户无感，但整个库的强度会随时间收敛到当前标准。

    ⚠ 结构判断必须在 int() 之前，而且是**独立返回**，不能串在 and 里。
      写成 `len(parts) >= 4 and int(parts[2]) < ROUNDS` 会漏掉最关键的一类：
      段数不足时 `and` 直接短路成 False，`except` 那支根本进不去 ——
      于是"解析不了的串"被判定为"不需要升级"，与下面的意图完全相反。
      （garbage → 不合法 → 应该重算，而不是保留一个永远校验不过的串。）
    """
    parts = (hashed or "").split("$")
    if len(parts) < 4:
        # 不是合法的 bcrypt 串（长度不对、被截断、干脆是明文），
        # 下次登录成功时顺手换成一个合法哈希
        return True
    try:
        return int(parts[2]) < BCRYPT_ROUNDS
    except ValueError:
        # $2b$xx$... 里 xx 不是数字 —— 同样算不合法
        return True


# ============================================================
# JWT
# ============================================================


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(
    user_id: uuid.UUID | str,
    *,
    token_type: TokenType,
    role: str = "user",
    ttl: timedelta | None = None,
) -> tuple[str, str, datetime]:
    """
    签一个 JWT。

    Returns:
        (token, jti, 过期时刻)。
        **jti 必须返回给调用方** —— refresh token 要用它写 Redis 白名单；
        自己从 token 里再解一次也能拿到，但那等于把"刚签好的东西"重新解析一遍，
        多一次出错机会。

    Claims 说明：
        sub  用户 id（字符串；JWT 规范要求 sub 是字符串，不是 UUID 对象）
        typ  access / refresh —— **必须校验**，否则 refresh 能被当 access 用，
             7 天的凭证就变成了 7 天的接口访问权
        jti  唯一 id，用于 refresh 白名单与重放检测
        role 角色快照。它的作用是"少查一次库"，代价是**改角色后要等 token 过期**
             （最长 2 小时）。对 admin 权限这种高危操作，接口层仍会回查库。
    """
    if token_type not in (TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH):
        raise ValueError(f"未知 token 类型：{token_type!r}")

    issued_at = _now()
    if ttl is None:
        ttl = (
            timedelta(minutes=settings.auth_access_ttl_min)
            if token_type == TOKEN_TYPE_ACCESS
            else timedelta(days=settings.auth_refresh_ttl_days)
        )
    expires_at = issued_at + ttl
    jti = uuid.uuid4().hex

    payload = {
        "sub": str(user_id),
        "typ": token_type,
        "jti": jti,
        "role": role,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, _secret(), algorithm=ALGORITHM)
    return token, jti, expires_at


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    """
    校验并解出 claims。

    Raises:
        TokenError: 签名不对 / 过期 / 类型不符 / 字段缺失
                    —— **一律同一个异常**。区分"过期"和"伪造"对攻击者有用、对我们没用；
                    前端只需要知道"这个 token 不能用了"。
    """
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],
            # 必须显式要求 exp：不传 expected 时 pyjwt 不校验过期时间
            options={"require": ["exp", "sub", "typ", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("登录已过期") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("凭证无效") from exc

    if payload.get("typ") != expected_type:
        # 这一条是硬红线：不校验的话，refresh token 可以直接当 access 用
        raise TokenError(f"凭证类型不符（需要 {expected_type}）")

    return payload


def token_user_id(payload: dict[str, Any]) -> uuid.UUID:
    """从 claims 里取 user id（统一在这里做格式转换与报错）。"""
    try:
        return uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenError("凭证缺少有效的用户标识") from exc
