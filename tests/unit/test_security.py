"""
密码学原语的单测（app/core/security.py + auth_service 的占位哈希）
==================================================================
这一层是**唯一**做密码学运算的地方，所以它的测试要比别处更"刁"：
每一条用例对应一种真实的失守方式，而不是"调一下、不报错"。

成本因子说明：所有用例都用 rounds=4（约 0.7ms）。生产的 12（166ms）会让
这一整个文件慢 20 秒，而**断言的内容完全一样** —— 成本因子写在哈希串里，
`verify_password` 会自己读出来，测试不需要真的用 12 才能证明逻辑正确。
唯一的例外是 `test_dummy_hash_uses_production_cost`：它断言占位哈希**必须**是 12。
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.core import security
from app.core.security import (
    BCRYPT_MAX_BYTES,
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
from app.services.auth_service import _DUMMY_HASH

USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ============================================================
# 密码哈希
# ============================================================


def test_hash_is_salted_and_verifiable():
    """同一个密码两次哈希结果不同（有盐），但都能验过。"""
    first = hash_password("correct horse", rounds=4)
    second = hash_password("correct horse", rounds=4)

    assert first != second, "两次哈希值相同 = 没有加盐，彩虹表可以一击命中"
    assert verify_password("correct horse", first)
    assert verify_password("correct horse", second)


def test_wrong_password_rejected():
    hashed = hash_password("right-password", rounds=4)
    assert verify_password("wrong-password", hashed) is False


def test_empty_inputs_rejected_without_raising():
    """空密码 / 空哈希都返回 False，不抛异常（脏数据不该变成 500）。"""
    assert verify_password("", hash_password("x" * 8, rounds=4)) is False
    assert verify_password("x" * 8, "") is False


def test_hash_rejects_empty_password():
    with pytest.raises(ValueError, match="不能为空"):
        hash_password("")


def test_hash_rejects_over_72_bytes_instead_of_truncating():
    """
    超过 bcrypt 的 72 字节上限时**必须抛错**，不能静默截断。

    为什么这条最重要：老库里常见"截断"实现，其后果是
      "密码的前 72 字节相同即视为同一个密码" —— 用户完全不知道，
      而攻击者可以直接构造一个更长但前缀相同的密码登进去。
    """
    ok = "a" * BCRYPT_MAX_BYTES
    too_long = "a" * (BCRYPT_MAX_BYTES + 1)

    assert hash_password(ok, rounds=4)          # 正好 72 字节合法
    with pytest.raises(ValueError, match="密码过长"):
        hash_password(too_long)

    # 中文一个字 3 字节：24 个字正好 72，25 个就超了
    assert hash_password("汉" * 24, rounds=4)
    with pytest.raises(ValueError):
        hash_password("汉" * 25)


def test_verify_rejects_over_long_input():
    """注册时拒绝超长密码，登录时也必须拒绝 —— 否则两边判定不一致。"""
    hashed = hash_password("a" * 72, rounds=4)
    assert verify_password("a" * 72, hashed) is True
    assert verify_password("a" * 73, hashed) is False


def test_corrupted_hash_returns_false():
    """手工改库改坏的哈希串 → 验证失败，而不是抛异常。"""
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "$2b$12$tooShort") is False


def test_needs_rehash_detects_lower_cost():
    """成本因子低于当前标准时提示升级；已经是高标准时不提示。"""
    assert needs_rehash(hash_password("pw12345678", rounds=4)) is True
    assert needs_rehash(hash_password("pw12345678", rounds=security.BCRYPT_ROUNDS)) is False
    # 解析不了的串按"需要升级"处理：下次登录成功时会重新哈希成合法格式
    assert needs_rehash("garbage") is True


def test_dummy_hash_uses_production_cost():
    """
    占位哈希必须是**格式合法、成本因子 12** 的真实 bcrypt 串。

    这是防时序侧信道的关键：登录时"用户不存在"也要跑一次 bcrypt，
    把耗时拉到与"密码错"同一量级。如果占位串是手写的假值，
    bcrypt 解析阶段就会报错并立刻返回 —— 那反而制造出更大的时间差，
    比不陪跑还糟（攻击者能直接按响应时间判断邮箱是否存在）。
    """
    assert _DUMMY_HASH.startswith("$2b$12$"), f"占位哈希成本因子不对：{_DUMMY_HASH[:7]}"

    started = time.perf_counter()
    assert verify_password("任何密码都不会匹配", _DUMMY_HASH) is False
    elapsed = time.perf_counter() - started

    # 成本因子 12 在本机约 166ms；给足余量但必须明显「不是立刻返回」
    assert elapsed > 0.05, f"占位哈希校验只花了 {elapsed * 1000:.1f}ms —— 没有真正跑 bcrypt"


# ============================================================
# JWT
# ============================================================


def test_token_roundtrip_carries_claims():
    token, jti, expires_at = create_token(USER_ID, token_type=TOKEN_TYPE_ACCESS, role="admin")
    payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)

    assert payload["sub"] == str(USER_ID)
    assert payload["typ"] == TOKEN_TYPE_ACCESS
    assert payload["role"] == "admin"
    assert payload["jti"] == jti
    assert token_user_id(payload) == USER_ID
    assert payload["exp"] == int(expires_at.timestamp())


def test_token_type_is_enforced():
    """
    refresh 不能当 access 用。

    漏了这条校验的后果不是"少一层检查"，而是**7 天的接口访问权**：
    refresh token 的有效期是 access 的 84 倍（7 天 vs 2 小时），
    一旦能互换，短有效期这个安全边界就完全消失了。
    """
    refresh, _, _ = create_token(USER_ID, token_type=TOKEN_TYPE_REFRESH)
    with pytest.raises(TokenError, match="类型不符"):
        decode_token(refresh, expected_type=TOKEN_TYPE_ACCESS)

    access, _, _ = create_token(USER_ID, token_type=TOKEN_TYPE_ACCESS)
    with pytest.raises(TokenError, match="类型不符"):
        decode_token(access, expected_type=TOKEN_TYPE_REFRESH)


def test_expired_token_rejected():
    """过期 token 必须拒绝（而不是"解出 claims 就好"）。"""
    from datetime import timedelta

    token, _, _ = create_token(
        USER_ID, token_type=TOKEN_TYPE_ACCESS, ttl=timedelta(seconds=-1)
    )
    with pytest.raises(TokenError, match="过期"):
        decode_token(token, expected_type=TOKEN_TYPE_ACCESS)


def test_tampered_signature_rejected():
    """改掉签名/载荷任何一个字节都必须失败。"""
    token, _, _ = create_token(USER_ID, token_type=TOKEN_TYPE_ACCESS)
    header, payload, signature = token.split(".")

    # ① 改签名
    with pytest.raises(TokenError):
        decode_token(f"{header}.{payload}.{signature[:-4]}AAAA", expected_type=TOKEN_TYPE_ACCESS)

    # ② 改载荷（把 sub 换成别人）
    import base64
    import json

    def b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    forged_payload = base64.urlsafe_b64decode(payload + "==")
    claims = json.loads(forged_payload)
    claims["sub"] = str(uuid.uuid4())
    with pytest.raises(TokenError):
        decode_token(f"{header}.{b64(claims)}.{signature}", expected_type=TOKEN_TYPE_ACCESS)


def test_other_secret_rejected(monkeypatch):
    """换一个签名密钥后，旧 token 立即失效（这就是"改 AUTH_SECRET_KEY 全员重登"的原理）。"""
    token, _, _ = create_token(USER_ID, token_type=TOKEN_TYPE_ACCESS)
    monkeypatch.setattr(security.settings, "auth_secret_key", "another-secret-value")
    monkeypatch.setattr(security, "_EPHEMERAL_SECRET", "")
    with pytest.raises(TokenError):
        decode_token(token, expected_type=TOKEN_TYPE_ACCESS)


def test_unknown_token_type_rejected():
    with pytest.raises(ValueError, match="未知 token 类型"):
        create_token(USER_ID, token_type="id_token")  # type: ignore[arg-type]


def test_token_user_id_rejects_bad_sub():
    with pytest.raises(TokenError):
        token_user_id({"sub": "not-a-uuid"})
    with pytest.raises(TokenError):
        token_user_id({})


def test_ephemeral_secret_is_generated_once(monkeypatch):
    """
    没配密钥时，进程内会生成一个临时值并**复用** —— 不能每次调用都换。

    每次调用都换的话，签出来的 token 立刻就无法验签（同一个进程内也登不上），
    症状是"登录成功但下一个请求就 401"，极难定位。
    """
    monkeypatch.setattr(security.settings, "auth_secret_key", "")
    monkeypatch.setattr(security, "_EPHEMERAL_SECRET", "")

    first = security._secret()
    second = security._secret()

    assert first and first == second, "临时密钥不稳定 —— 同进程内签发的 token 会立刻失效"
    assert len(first) >= 32, "临时密钥太短，HS256 的强度取决于密钥熵"
