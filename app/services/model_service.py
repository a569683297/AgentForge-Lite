"""
模型注册表服务（模型切换需求 S1）
==================================
职责三块：
    ① 列表合流：内置模型（.env 驱动）+ 自定义模型（llm_models 表）→ GET /api/models
    ② 自定义模型 CRUD（写路径）
    ③ 连通性测试（运维诊断 —— **故意不走 LLM Gateway**，理由见 _probe_chat_endpoint）

安全约定（需求稿 §4.4 已拍板）：
    · api_key **明文入库**（本地自托管项目，DB 就在本机 —— 已知限制）；
    · 出口一律走 `mask_key()` 打码，**任何函数都不返回明文 key**；
    · 日志与异常消息**不打 key**（本文件所有 logger 调用只带 base_url / model / 错误）。
"""

import time
import uuid
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.config import LLM_CHANNELS, settings
from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.llm_model import LlmModel

# 内置通道的显示名（只影响 UI 文案，不影响任何逻辑）。
# `openai` 在本项目指"OpenAI 兼容协议通道"（.env 里实际配的可能是中转站）——
# 显示名保留"兼容"二字，诚实反映它的本质是协议而不是某个具体厂商。
BUILTIN_LABELS: dict[str, str] = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI 兼容",
}

# 判定"本地端点"的主机名集合：命中这些 → kind=local（前端显示「本地」标签）。
# host.docker.internal 是 Docker 容器访问宿主机的特殊域名（Ollama 场景的实测口，
# 见 docs/本地模型接入方案-2026-10-12.md §二）；.local 结尾的主机名单独判。
_LOCAL_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
)


# ============================================================
# 一、纯函数（无 IO，单测直接打）
# ============================================================
def mask_key(key: str | None) -> str | None:
    """
    key 打码：保留前 3 + 后 4 位，中间打掉。

    为什么"前 3 + 后 4"：打码要同时满足两个目的 ——
      ① 能**辨认**（用户对比几个 key 时看得出是哪一个：后 4 位是常见辨识位）；
      ② 不能**复原**（中间几十位是关键熵，全部打掉）。
    太短的 key（≤8 位）连前后缀都可能泄露，整体打掉。
    """
    if not key:
        return None
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def classify_kind(base_url: str) -> str:
    """
    自定义模型按 base_url 的主机名归类："local"（本机/宿主机）或 "custom"。

    为什么用主机名判定而不是让用户自己选：这是**可机器判定的事实**
    （地址指向哪台机器），不是主观标签 —— 让用户选反而多一个会填错的字段。
    """
    host = (urlparse(base_url).hostname or "").lower()
    if host in _LOCAL_HOSTNAMES or host.endswith(".local"):
        return "local"
    return "custom"


def _extract_error_detail(resp: httpx.Response) -> str:
    """从错误响应体里抽一句可读信息（OpenAI 风格 error.message / FastAPI 风格 detail）。"""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 —— 错误体不是 JSON 很正常，不值得再抛
        return ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
        if body.get("detail"):
            return str(body["detail"])[:200]
    return ""


def _explain_probe_error(exc: Exception) -> str:
    """
    把机器异常翻译成"人能直接行动"的一句话（纯函数，可单测）。

    为什么值得单独写：连通性测试的价值几乎全在**失败时**（成功不用解释）。
    原始异常（`ConnectError: [Errno 61] Connection refused`）对用户是黑话；
    而 401 / 404 / 超时各自对应**完全不同的修法**，必须分开说清。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = _extract_error_detail(exc.response)
        if code in (401, 403):
            base = "API Key 无效或未授权（401/403）—— 检查 Key 是否正确、是否有多余空格"
        elif code == 404:
            base = "端点路径不存在（404）—— 检查 Base URL 是否包含 /v1 这类前缀"
        elif code == 429:
            base = "请求被限流（429）—— 稍后重试"
        elif code >= 500:
            base = (
                f"端点或网络中继错误（{code}）—— 可能是端点服务异常，"
                f"也可能是网络中间层（代理/网关）拦截"
            )
        else:
            base = f"请求被拒绝（{code}）"
        return f"{base}：{detail}" if detail else base
    if isinstance(exc, httpx.ConnectError):
        return "无法连接 —— 检查地址是否正确、端点服务是否已启动"
    if isinstance(exc, httpx.TimeoutException):
        return f"连接超时（{_PROBE_TIMEOUT_S:.0f}s 无响应）—— 端点不可达或过慢"
    if isinstance(exc, httpx.HTTPError):
        return f"网络层错误：{exc}"
    return f"未预期错误：{exc}"


# ============================================================
# 二、列表合流
# ============================================================
def _builtin_entries() -> list[dict]:
    """
    内置模型清单 —— 来源是 .env（settings），**没配 key 的通道不列出**。

    为什么没 key 就不列（而不是列出但置灰）：列出来它也用不了，
    点了发问只会报错 —— 不如压根不出现（`.env` 加 key 后刷新即见）。
    """
    entries: list[dict] = []
    for channel in LLM_CHANNELS:
        cfg = settings.llm_channel(channel)
        if not cfg["api_key"]:
            continue
        entries.append(
            {
                "id": channel,
                "name": BUILTIN_LABELS.get(channel, channel),
                "model": cfg["model"],
                "kind": "builtin",
                "enabled": True,
                "base_url": cfg["base_url"],
                "api_key_masked": mask_key(cfg["api_key"]),
            }
        )
    return entries


def _to_out(row: LlmModel) -> dict:
    """ORM 行 → 响应字典（唯一的出口转换点：**打码只在这里发生一次**）。"""
    return {
        "id": str(row.id),
        "name": row.name,
        "model": row.model,
        "kind": classify_kind(row.base_url),
        "enabled": row.enabled,
        "base_url": row.base_url,
        "api_key_masked": mask_key(row.api_key),
    }


async def list_models() -> list[dict]:
    """内置（.env）+ 自定义（DB）合流。顺序：内置在前，自定义按创建时间。"""
    async with async_session_factory() as session:
        rows = (
            (await session.execute(select(LlmModel).order_by(LlmModel.created_at)))
            .scalars()
            .all()
        )
    return _builtin_entries() + [_to_out(row) for row in rows]


# ============================================================
# 三、自定义模型 CRUD
# ============================================================
async def create_model(payload) -> dict:
    """新建自定义模型。`payload` 是 LlmModelCreate（base_url 已由校验器规范化）。"""
    async with async_session_factory() as session:
        row = LlmModel(
            name=payload.name,
            base_url=payload.base_url,
            api_key=payload.api_key or None,   # 空字符串统一存 NULL（语义一致）
            model=payload.model,
        )
        session.add(row)
        await session.commit()
        # expire_on_commit=False（见 core/db.py）：commit 后属性依然可读，不用 refresh
        return _to_out(row)


async def get_model(model_id: uuid.UUID) -> LlmModel | None:
    """按 id 取一行（S2 的网关解析与 test/{id} 接口都会用）。"""
    async with async_session_factory() as session:
        return await session.get(LlmModel, model_id)


async def update_model(model_id: uuid.UUID, payload) -> dict | None:
    """
    编辑。只改"传了的字段"；`api_key` 传 None / "" 都表示**不修改**
    （前端编辑时不回显明文 key，必须留空 = 保留，否则一编辑就清 key —— 见 schemas 模块注释）。
    """
    async with async_session_factory() as session:
        row = await session.get(LlmModel, model_id)
        if row is None:
            return None
        if payload.name is not None:
            row.name = payload.name
        if payload.base_url is not None:
            row.base_url = payload.base_url
        if payload.model is not None:
            row.model = payload.model
        if payload.enabled is not None:
            row.enabled = payload.enabled
        if payload.api_key:
            row.api_key = payload.api_key
        await session.commit()
        return _to_out(row)


async def delete_model(model_id: uuid.UUID) -> bool:
    """删除。返回是否删到了东西（False → 路由层回 404）。"""
    async with async_session_factory() as session:
        row = await session.get(LlmModel, model_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


# ============================================================
# 四、连通性测试
# ============================================================
_PROBE_TIMEOUT_S = 15.0


def build_outbound_client(base_url: str, *, timeout: float) -> httpx.AsyncClient:
    """
    给某个 LLM 端点构造 httpx client：**本机/内网地址绕过系统代理**，其余跟随环境。

    ★ 策略的唯一出处，两个使用方共用（S2 归一）：
      · 连通性探测（本模块 `_probe_chat_endpoint`）；
      · 对话网关（llm_gateway 的 `_call_once` / `_call_once_stream`）——
        S1 只修了探测路径，S2 发现对话路径会踩**同一个坑**：
        用户把 Ollama（host.docker.internal:11434）录入为自定义模型后，
        网关若仍 trust_env=True，代理**不认识这个域名** → 必然怪异失败。
    踩坑就地修一次、两条路径同时生效，不留半修状态。

    为什么必须分叉（真实踩坑，2026-10-13 冒烟暴露）：
    本机 env 配了 HTTP_PROXY/HTTPS_PROXY（127.0.0.1:56908）。httpx 默认
    `trust_env=True` —— 于是"探测 127.0.0.1:9（端口没开）"的请求被代理接走，
    代理回一个 **502**；用户看到的文案变成"端点内部错误（502）"，
    而真实原因只是"这个端口没有服务"。**错误翻译被代理短路、且指向了错误的方向。**
    本机/内网地址（localhost / 127.0.0.1 / host.docker.internal / *.local）
    经公共代理转发毫无意义 —— 显式直连（trust_env=False）。
    （同一个坑的另一面：识别特征是"多个不同端口返回同一个错误码"= 中间层应答，
    见 D25 复盘；`scripts/dify_bridge.py` 里也有同款 trust_env=False 的先例。）
    """
    if classify_kind(base_url) == "local":
        return httpx.AsyncClient(timeout=timeout, trust_env=False)
    # 外网端点仍跟随环境代理：那是用户层面的网络策略（如科学上网/中转），必须尊重
    return httpx.AsyncClient(timeout=timeout)


async def _probe_chat_endpoint(base_url: str, api_key: str | None, model: str) -> dict:
    """
    对端点发一次**最小真实请求**（"ping"），返回 {"ok", "latency_ms", "sample"}；
    失败抛原始 httpx 异常，由 `test_connection` 翻译。

    它是本模块**唯一碰网络的点** —— 测试里 monkeypatch 掉它，
    就能在没有网络的环境下验证整条上层链路（接口形状 / 错误翻译 / 包装逻辑）。

    ★ 关于架构红线（"业务代码不得绕过 Gateway 直连 SDK"）——这里为什么是合规的：
      · 本函数是**运维诊断**，不是业务 LLM 调用：不产生回答、不供用户消费；
      · 走网关会有两个实质性错误：① 每次点"测试连接"都会在 Langfuse 里
        多一条 generation 记录（观测数据混进噪音）；② 网关带**降级链**，
        而测试要回答的是"**我这个配置**通不通" —— 降级后测的是另一个通道，
        答非所问。所以这里用裸 httpx：探测的就是"这个地址 + 这个 key + 这个模型"本身。
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        # 本地端点（如 Ollama）不需要 key —— 与其带一个假的 Bearer 头，不如不带
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,   # 只要一个 token：测试连通性，不是测试生成质量
    }

    start = time.perf_counter()
    async with build_outbound_client(base_url, timeout=_PROBE_TIMEOUT_S) as client:
        resp = await client.post(
            f"{base_url}/chat/completions", headers=headers, json=payload
        )
        resp.raise_for_status()   # 非 2xx 抛 HTTPStatusError（由翻译层按码分支）
        data = resp.json()
    latency_ms = int((time.perf_counter() - start) * 1000)

    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {"ok": True, "latency_ms": latency_ms, "sample": content[:50] or None}


async def test_connection(base_url: str, api_key: str | None, model: str) -> dict:
    """
    连通性测试的完整出口：探测 + 错误翻译。**永不抛异常**（失败也是一种结果）。

    为什么全兜异常：本接口要回答的问题就是"通不通"——
    失败是**预期内的正常返回**（ok=false），不是服务端故障。
    若让它冒泡成 500，前端就得在两处（HTTP 错误 / 响应体）处理同一件事。
    """
    try:
        result = await _probe_chat_endpoint(base_url, api_key, model)
        return {"ok": True, "latency_ms": result["latency_ms"], "sample": result["sample"], "error": None}
    except Exception as exc:  # noqa: BLE001 —— 故意全兜（见 docstring）；日志不打 key
        logger.info(
            "模型连通性测试失败 base_url=%s model=%s err=%s",
            base_url,
            model,
            exc.__class__.__name__,
        )
        return {"ok": False, "latency_ms": None, "sample": None, "error": _explain_probe_error(exc)}
