"""
账号体系的数据契约
==================
只放**跨层传输**的结构；密码哈希、JWT 这些原语不在这里，在 `app/core/security.py`。

三条贯穿全文件的约束：

1. **响应模型里永远没有 `password_hash`**
   这不是靠"记得别写"，而是靠**结构上不给**：`UserOut` 没有这个字段，
   所以任何 `UserOut(**user.__dict__)` 都不可能把它带出去。想泄露得先改 schema。

2. **密码字段的 `max_length` 是 72，与 bcrypt 的字节上限对齐**
   但注意：`max_length` 数的是**字符**，而 bcrypt 限的是**字节**。
   中文密码 72 个字符 = 216 字节，仍会超。所以真正的拒绝在
   `security.hash_password` 里（按字节判断），schema 这层只是挡掉明显过长的输入。

3. **邮箱统一小写**
   在 `normalize_email` 校验器里做，而不是在各处调用前手动 `.lower()` ——
   漏一处就会造出"注册时大写、登录时小写"查不到人的经典问题。
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# 邮箱校验：不用 email-validator（多一个依赖、且它接受一堆我们不想处理的形态）。
# 这条正则只保证"有一个 @、两边非空、域名带点"——够用，且不会误杀企业邮箱。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    """去空格 + 转小写 + 格式校验。所有入口共用这一个函数。"""
    email = (value or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("邮箱格式不正确")
    if len(email) > 255:
        raise ValueError("邮箱过长")
    return email


# ============================================================
# 请求
# ============================================================


class RegisterRequest(BaseModel):
    email: str = Field(description="登录邮箱（服务端会转小写）")
    password: str = Field(min_length=8, max_length=72, description="密码，至少 8 位")
    display_name: str = Field(default="", max_length=64, description="显示名，可空")

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=72)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        # 注意：登录时**不能**因为"格式不对"就给出不同的错误提示，
        # 否则等于告诉攻击者"这个邮箱根本没注册过"。校验只做归一化，
        # 真正的判定统一在 service 层返回"邮箱或密码错误"。
        return (value or "").strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return (value or "").strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, description="邮件/日志里拿到的一次性令牌")
    new_password: str = Field(min_length=8, max_length=72)


# ============================================================
# 响应
# ============================================================


class UserOut(BaseModel):
    """
    对外可见的用户信息。**没有 password_hash，也没有任何派生字段**。

    `is_active` 是给管理员看"这人还能不能登录"；普通用户看到自己的 true 也无害。
    """

    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None

    model_config = {"from_attributes": True}


class TokenPairOut(BaseModel):
    """
    登录/刷新的返回体。

    为什么 access 与 refresh 分开、且都要返回过期时间：
      前端需要在过期**前**静默刷新，而不是等 401 再重试 ——
      后者会让用户看到一次失败（哪怕自动恢复了）。
    `expires_in` 是 access 的剩余秒数（OAuth2 的惯例字段名）。
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="access token 有效秒数")
    user: UserOut


class MessageOut(BaseModel):
    """一句人话结果（登出、改密这类没有实体返回的接口）。"""

    message: str


class ForgotPasswordOut(BaseModel):
    """
    找回密码的返回体。

    `dev_link` 只在**未配置 SMTP 且 app_env=dev** 时出现 —— 本地能走通全流程。
    生产环境它必须是 null，否则等于把重置令牌直接发给任何知道邮箱的人。
    """

    message: str
    dev_link: str | None = Field(
        default=None, description="仅开发环境且未配邮件时返回，生产恒为 null"
    )
