"""
单元：模型注册表的纯逻辑（打码 / 本地归类 / 错误翻译 / 探测包装）
==================================================================
为什么这四块单独测：连通性测试的**全部用户价值在失败分支**（成功不用解释），
而失败信息的质量取决于两件事 —— 异常类型分得对不对（_explain_probe_error）、
探测点是不是唯一碰网络的（_probe_chat_endpoint）。
这两件事都能在**完全不碰网络**的前提下测掉。

纪律（与全项目一致）：
· 不碰网络 —— `_probe_chat_endpoint` 在用例里被替换（它才是唯一真发请求的点）；
· 不写库 —— 写库链路在 integration/test_models_api.py；
· 断言按"用户能不能据此行动"打 —— 例如 404 的文案必须**指向**"检查 /v1 前缀"，
  而不是只断言"报错了"。
"""

import httpx
import pytest

from app.services import model_service

# ============================================================
# 一、mask_key：口径 = 前 3 + 后 4，太短整体打掉
# ============================================================


def test_mask_key_keeps_head_and_tail():
    """常规 key：前 3 位 + 后 4 位可辨识，中间全部打掉。"""
    assert model_service.mask_key("sk-abcdef123456") == "sk-***3456"


def test_mask_key_short_key_fully_masked():
    """≤8 位时连前后缀都是大头，整体打掉（否则等于泄露一半）。"""
    assert model_service.mask_key("short123") == "***"


def test_mask_key_none_and_empty():
    assert model_service.mask_key(None) is None
    assert model_service.mask_key("") is None


# ============================================================
# 二、classify_kind：本机/宿主机地址 → local
# ============================================================


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000/v1",
        "http://host.docker.internal:11434/v1",   # Ollama 场景的实测口
        "http://my-box.local/v1",
    ],
)
def test_classify_kind_detects_local(url):
    assert model_service.classify_kind(url) == "local"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "https://api.deepseek.com",
        "https://gateway.company.cn/v1",
    ],
)
def test_classify_kind_defaults_to_custom(url):
    assert model_service.classify_kind(url) == "custom"


# ============================================================
# 三、_explain_probe_error：每种失败给出"指向修法"的句子
# ============================================================
def _status_error(code: int, body: dict | None = None) -> httpx.HTTPStatusError:
    """构造一个带响应体的 HTTPStatusError（模拟真实端点的报错）。"""
    request = httpx.Request("POST", "http://example.com/v1/chat/completions")
    response = httpx.Response(code, json=body or {}, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


def test_connect_error_points_to_service_and_address():
    msg = model_service._explain_probe_error(httpx.ConnectError("connection refused"))
    assert "无法连接" in msg
    assert "地址" in msg and "启动" in msg


def test_timeout_is_distinguished_from_connect_error():
    """超时与连不上**是两回事**（一个可能过慢、一个彻底到不了），文案必须不同。"""
    msg = model_service._explain_probe_error(httpx.TimeoutException("timed out"))
    assert "超时" in msg


def test_401_carries_endpoint_detail():
    """401：确认是 key 问题，并把端点返回的 detail（如 Invalid API key）带出来。"""
    err = _status_error(401, {"error": {"message": "Invalid API key provided"}})
    msg = model_service._explain_probe_error(err)
    assert "API Key" in msg
    assert "Invalid API key provided" in msg


def test_404_points_to_v1_prefix():
    """404 的**指定修法**：查 Base URL 是否漏了 /v1 —— 这是最常见的填错。"""
    msg = model_service._explain_probe_error(_status_error(404, {"detail": "Not Found"}))
    assert "404" in msg
    assert "/v1" in msg


def test_5xx_is_endpoint_or_relay_not_user_config():
    """5xx 要明说"不是你的配置错"、并把"代理/网关拦截"这种中间层可能列进来
    —— 本机冒烟实测：不绕过代理时，"端口没开"会被代理翻译成 502（见 _build_probe_client）。"""
    msg = model_service._explain_probe_error(_status_error(503))
    assert "端点或网络中继错误" in msg
    assert "代理" in msg


# ============================================================
# 三点五、_build_probe_client：本机地址绕代理、外网跟随环境
# ============================================================


def test_probe_client_bypasses_proxy_for_local_addresses():
    """本机地址必须 trust_env=False（否则"端口没开"会被代理翻译成 502 —— 真实踩坑）。"""
    assert model_service._build_probe_client("http://127.0.0.1:9/v1").trust_env is False
    assert model_service._build_probe_client("http://host.docker.internal:11434/v1").trust_env is False


def test_probe_client_keeps_env_proxy_for_remote_addresses():
    """外网端点跟随环境代理（那是用户层面的网络策略，如科学上网/中转）。"""
    assert model_service._build_probe_client("https://api.example.com/v1").trust_env is True


# ============================================================
# 四、test_connection：永远返回结果、永不抛异常
# ============================================================


async def test_test_connection_success_shape(monkeypatch):
    async def fake_probe(base_url, api_key, model):
        return {"ok": True, "latency_ms": 42, "sample": "pong"}

    monkeypatch.setattr(model_service, "_probe_chat_endpoint", fake_probe)
    result = await model_service.test_connection("https://api.example.com/v1", "sk-x", "m")

    assert result == {"ok": True, "latency_ms": 42, "sample": "pong", "error": None}


async def test_test_connection_failure_is_result_not_exception(monkeypatch):
    """失败是**预期内的正常返回**（ok=false）—— 探测抛出的原始异常必须被翻译，不许冒泡。"""

    async def fake_probe(base_url, api_key, model):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(model_service, "_probe_chat_endpoint", fake_probe)
    result = await model_service.test_connection("http://localhost:9/v1", None, "m")

    assert result["ok"] is False
    assert result["latency_ms"] is None
    assert "无法连接" in result["error"]
