"""
LLM Gateway（D7 版本：双通道降级 + Langfuse 追踪）
=====================================================
统一管理所有 LLM 调用，业务代码不得直连 SDK（架构红线）。

D5：单通道 + Langfuse 追踪
D7：双通道降级（主通道失败 → 自动切备用通道）

降级流程：
chat() → 尝试主通道 → 成功返回
                  → 失败（异常/超时/400）→ 记日志 → 切备用通道
                  → 备用也失败 → 抛异常（两个通道都挂了才失败）

Langfuse v4 API（已查证 SDK 4.15.1 源码）：
- start_as_current_observation 是上下文管理器，进入自动开始、退出自动 end
- as_type="generation" = LLM 生成调用；gen.update() 记录输出/token
"""

import time
from typing import Any

import httpx
from langfuse import Langfuse

from app.config import settings
from app.core.logging import logger


# ---- Langfuse 客户端（单例，懒连接）----
langfuse = Langfuse(
    public_key=settings.langfuse_public_key,
    secret_key=settings.langfuse_secret_key,
    host=settings.langfuse_host,
)


# ============================================================
# 内部：单通道调用（主备共用）
# ============================================================
async def _call_once(
    provider: dict,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    trace_name: str,
    gen: Any,  # langfuse observation 对象
) -> dict:
    """
    用指定 provider 调用一次 LLM，返回解析后的响应数据。

    Args:
        provider: {"api_key", "base_url", "model"} 通道配置
        tools: 传了就走 function calling，否则普通 chat
        gen: Langfuse observation（由调用方创建，这里只负责 update）
    Returns:
        {"reply": str} 或 {"message": dict}（取决于是否带 tools）
    """
    start = time.perf_counter()
    payload: dict[str, Any] = {
        "model": provider["model"],
        "messages": messages,
        "temperature": 0.4,
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{provider['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {provider['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()  # 非 2xx 抛异常（触发降级）
        data = resp.json()

    usage = data.get("usage", {})

    # 记录到 Langfuse（模型名/输出/token）
    output: dict = {}
    if tools:
        msg = data["choices"][0]["message"]
        output = {"message": msg}
    else:
        msg = data["choices"][0]["message"]
        output = {"reply": msg["content"]}
    gen.update(
        output=output,
        usage_details={
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "total": usage.get("total_tokens", 0),
        },
    )
    logger.info(
        "LLM 调用成功 model=%s tokens=%s 耗时=%.2fs",
        provider["model"],
        usage.get("total_tokens", 0),
        time.perf_counter() - start,
    )
    return {"data": data, "usage": usage}


# ============================================================
# 降级循环：主通道 → 失败 → 备用通道 → 失败 → 抛错
# ============================================================
async def _call_with_failover(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    trace_name: str,
) -> dict:
    """带降级的调用。返回 {"data": ..., "usage": ..., "provider": 实际使用的通道}。"""
    providers = [settings.primary_llm]
    backup = settings.backup_llm
    if backup:
        providers.append(backup)

    last_error: Exception | None = None

    for idx, provider in enumerate(providers):
        if not provider.get("api_key"):
            continue  # 该通道无 key，跳过

        provider_label = "主通道" if idx == 0 else f"备用通道({provider['model']})"
        # 每个通道独立创建 observation（Langfuse 能看到哪个通道被调用）
        with langfuse.start_as_current_observation(
            name=trace_name,
            as_type="generation",
            model=provider["model"],
            model_parameters={"temperature": 0.4, **({"tools": tools} if tools else {})},
            input={"messages": messages},
            metadata={"provider": provider_label},
        ) as gen:
            try:
                result = await _call_once(
                    provider, messages, tools=tools, trace_name=trace_name, gen=gen
                )
                result["provider"] = provider_label
                return result
            except Exception as e:
                last_error = e
                gen.update(level="ERROR", status_message=f"{provider_label} 失败: {e}")
                logger.warning(
                    "%s 调用失败，%s",
                    provider_label,
                    "切换备用通道" if idx < len(providers) - 1 else "无可用通道",
                )

    # 所有通道都失败
    raise RuntimeError(f"所有 LLM 通道均失败: {last_error}") from last_error


# ============================================================
# 对外 API（保持签名不变，调用方零改动）
# ============================================================
async def chat(
    messages: list[dict],
    *,
    trace_name: str = "llm-chat",
    user_id: str | None = None,
) -> str:
    """发送对话到 LLM（自动降级）。返回回复文本。"""
    result = await _call_with_failover(messages, trace_name=trace_name)
    return result["data"]["choices"][0]["message"]["content"]


async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    trace_name: str = "llm-tools",
) -> dict:
    """发送对话到 LLM，支持 function calling（自动降级）。

    Returns:
        {"message": {role, content, tool_calls?}, "usage": {...}}
    """
    result = await _call_with_failover(messages, tools=tools, trace_name=trace_name)
    return {"message": result["data"]["choices"][0]["message"], "usage": result["usage"]}
