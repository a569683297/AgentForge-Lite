"""
集成：模型注册表接口（真打 FastAPI 应用；**写库自清理**）
==========================================================
标记 `db`（需要真实 Postgres）。与其他集成测试的差别：本组用例**写库**
—— CRUD 正是要验的东西，把 DB 换成 mock 等于把要验的东西删掉了。
代价是纪律：**每个用例用唯一名称创建、finally 里保证删除**，不污染共享数据。

不打网络：连通性测试的探测点（model_service._probe_chat_endpoint）在用例里被替换，
失败分支用注入的 httpx 异常驱动 —— CI 无外网也能跑完整条链路。
"""

import uuid

import httpx
import pytest

from app.config import settings
from app.services import model_service

pytestmark = pytest.mark.db


def _unique(prefix: str) -> str:
    """唯一显示名：并行/重跑不互撞，事后也便于辨认残留。"""
    return f"pytest-{prefix}-{uuid.uuid4().hex[:8]}"


async def _create(api_client, **overrides) -> dict:
    """建一条自定义模型（默认值可被 overrides 覆盖），返回响应 body。"""
    payload = {
        "name": _unique("模型"),
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test-abcdef123456",
        "model": "test-model",
    }
    payload.update(overrides)
    resp = await api_client.post("/api/models", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _safe_delete(api_client, model_id: str) -> None:
    """清理用：删没删到都容忍（清理路径不抛，避免遮蔽真正的断言失败）。"""
    try:
        await api_client.delete(f"/api/models/{model_id}")
    except Exception:  # noqa: BLE001
        pass


# ============================================================
# 一、创建：打码 + 规范化 + 归类
# ============================================================


async def test_create_masks_key_and_normalizes_base_url(api_client):
    created = await _create(api_client, base_url="https://api.example.com/v1/")  # 故意带尾斜杠
    try:
        assert created["kind"] == "custom"
        assert created["enabled"] is True
        # 尾斜杠被去掉（写入时规范化 —— 防网关拼出 //chat/completions）
        assert created["base_url"] == "https://api.example.com/v1"
        # key 打码：前 3 + 后 4
        assert created["api_key_masked"] == "sk-***3456"
    finally:
        await _safe_delete(api_client, created["id"])


async def test_plaintext_key_never_appears_in_list_response(api_client):
    """★ 安全断言：列表响应**原文**里不得出现任何明文 key（不只查打码字段）。"""
    secret = "sk-test-abcdef123456"
    created = await _create(api_client, api_key=secret)
    try:
        resp = await api_client.get("/api/models")
        assert resp.status_code == 200
        assert secret not in resp.text
        # 打码值必须在（证明"不是把 key 字段整个吞了"，而是真打了码）
        mine = next(m for m in resp.json()["models"] if m["id"] == created["id"])
        assert mine["api_key_masked"] == "sk-***3456"
    finally:
        await _safe_delete(api_client, created["id"])


async def test_host_docker_internal_is_classified_local(api_client):
    created = await _create(
        api_client,
        base_url="http://host.docker.internal:11434/v1",
        api_key=None,   # 本地端点不需要 key
        model="qwen2.5:7b",
    )
    try:
        assert created["kind"] == "local"
        assert created["api_key_masked"] is None
    finally:
        await _safe_delete(api_client, created["id"])


# ============================================================
# 二、CRUD 全程（创建 → 列表 → 编辑 → 删除）
# ============================================================


async def test_crud_roundtrip(api_client):
    name = _unique("crud")
    created = await _create(api_client, name=name)
    model_id = created["id"]
    try:
        # 列表里有它
        listed = {m["id"]: m for m in (await api_client.get("/api/models")).json()["models"]}
        assert model_id in listed
        assert listed[model_id]["name"] == name

        # PATCH：改名 + 停用；**不带 api_key**（留空 = 不修改）
        new_name = f"{name}-改"
        patched = await api_client.patch(
            f"/api/models/{model_id}", json={"name": new_name, "enabled": False}
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["name"] == new_name
        assert body["enabled"] is False
        # key 没被动过 → 打码值保持不变（"留空=不修改"的核心断言）
        assert body["api_key_masked"] == "sk-***3456"

        # DELETE → 204；列表里消失
        assert (await api_client.delete(f"/api/models/{model_id}")).status_code == 204
        after = {m["id"] for m in (await api_client.get("/api/models")).json()["models"]}
        assert model_id not in after
    finally:
        await _safe_delete(api_client, model_id)


# ============================================================
# 三、校验与错误码（422 / 404）
# ============================================================


@pytest.mark.parametrize("bad_url", ["ftp://api.example.com", "api.example.com", "http:/broken"])
async def test_invalid_base_url_rejected_with_422(api_client, bad_url):
    resp = await api_client.post(
        "/api/models",
        json={"name": _unique("bad"), "base_url": bad_url, "model": "m"},
    )
    assert resp.status_code == 422


async def test_non_uuid_model_id_rejected_with_422(api_client):
    """model_id 声明为 UUID → 非法值由 FastAPI 直接 422（不用自己写正则，D14 的手法）。"""
    assert (await api_client.patch("/api/models/not-a-uuid", json={"name": "x"})).status_code == 422
    assert (await api_client.delete("/api/models/not-a-uuid")).status_code == 422


async def test_missing_model_returns_404(api_client):
    ghost = str(uuid.uuid4())
    assert (await api_client.patch(f"/api/models/{ghost}", json={"name": "x"})).status_code == 404
    assert (await api_client.delete(f"/api/models/{ghost}")).status_code == 404
    assert (await api_client.post(f"/api/models/{ghost}/test")).status_code == 404


# ============================================================
# 四、连通性测试接口（探测点被替换，不打网络）
# ============================================================


async def test_unsaved_test_endpoint_success(api_client, monkeypatch):
    async def fake_probe(base_url, api_key, model):
        return {"ok": True, "latency_ms": 42, "sample": "pong"}

    monkeypatch.setattr(model_service, "_probe_chat_endpoint", fake_probe)
    resp = await api_client.post(
        "/api/models/test",
        json={"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "m"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "latency_ms": 42, "sample": "pong", "error": None}


async def test_saved_test_endpoint_translates_failure(api_client, monkeypatch):
    """已保存模型的测试：探测抛错 → 200 + ok=false + 人话文案（**不是 500**）。"""
    created = await _create(api_client)
    try:

        async def fake_probe_fail(base_url, api_key, model):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(model_service, "_probe_chat_endpoint", fake_probe_fail)
        resp = await api_client.post(f"/api/models/{created['id']}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "无法连接" in body["error"]
    finally:
        await _safe_delete(api_client, created["id"])


# ============================================================
# 五、内置模型清单规则（.env 有 key 才列出）
# ============================================================


async def test_builtin_entries_follow_env_keys(api_client, monkeypatch):
    """
    内置模型来自 .env：**没配 key 的通道不列出**（列出也用不了，点开发问只会报错）。
    用 monkeypatch 变换 key 状态来验证规则本身 —— 不依赖本机 .env 的真实内容（CI 环境无关）。
    """
    secret = "sk-fake-main-key-1234"

    # 两个通道都断 key → 列表里没有任何 builtin
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    models = (await api_client.get("/api/models")).json()["models"]
    assert all(m["kind"] != "builtin" for m in models)

    # 给 deepseek 配上 key → 出现、打码、明文不出现在响应里
    monkeypatch.setattr(settings, "deepseek_api_key", secret)
    resp = await api_client.get("/api/models")
    builtins = [m for m in resp.json()["models"] if m["kind"] == "builtin"]
    assert [m["id"] for m in builtins] == ["deepseek"]
    assert builtins[0]["api_key_masked"] == "sk-***1234"
    assert secret not in resp.text
