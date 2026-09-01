"""
LLM Gateway（D5 版本：单次调用 + Langfuse v4 追踪）
=====================================================
统一管理所有 LLM 调用，业务代码不得直连 SDK（架构红线）。

D5 目标：让一次 LLM 调用出现在 Langfuse 面板
D7 升级：加双通道降级（DeepSeek 主 / OpenAI 备）

Langfuse v4 API 用法（已查证 SDK 4.15.1 源码）：
- start_as_current_observation(name=..., as_type="generation") 是上下文管理器
- as_type="generation" = 一次 LLM 生成调用（专有类型，带 token 计数）
- 用 with 块包裹：进入自动开始，退出自动 end
"""

import time

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


async def chat(
    messages: list[dict],
    *,
    trace_name: str = "llm-chat",
    user_id: str | None = None,
) -> str:
    """
    发送对话到 LLM（带 Langfuse 追踪）。

    Args:
        messages: OpenAI 格式消息列表 [{role, content}, ...]
        trace_name: Langfuse trace 名称
        user_id: 可选，通过 metadata 记录（v4 无直接 user 参数）
    Returns:
        LLM 回复文本
    """
    provider = settings.primary_llm
    if not provider.get("api_key"):
        raise ValueError("LLM API key 未配置，请检查 .env")

    start = time.perf_counter()

    # v4 上下文管理器：进入 with = 开始 observation，退出 = 自动 end
    # as_type="generation" = 标记这是一次 LLM 生成调用（面板按类型着色）
    with langfuse.start_as_current_observation(
        name=trace_name,
        as_type="generation",
        model=provider["model"],
        model_parameters={"temperature": 0.4},
        input={"messages": messages},
        metadata={"user_id": user_id} if user_id else None,
    ) as gen:
        try:
            # 真实调用 DeepSeek（OpenAI 兼容协议）
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "messages": messages,
                        "temperature": 0.4,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            reply = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            # 用 update() 统一记录输出 + token 计数（v4 API）
            gen.update(
                output={"reply": reply},
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
            return reply

        except Exception as e:
            # 失败也要记录（面板能看到失败调用）
            gen.update(level="ERROR", status_message=str(e))
            logger.error("LLM 调用失败: %s", e)
            raise


async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    trace_name: str = "llm-tools",
) -> dict:
    """
    发送对话到 LLM，支持官方 function calling（工具调用）。

    与 chat() 的区别：请求带 tools schema，LLM 可返回结构化 tool_calls
    （而不是文本 JSON）。这是 OpenAI 兼容 API 的标准工具调用协议。

    Returns:
        {"message": {role, content, tool_calls?}, "usage": {...}}
        - 有工具调用时 message["tool_calls"] = [{id, function:{name, arguments}}]
        - 无工具调用时 message["tool_calls"] 为 None
    """
    provider = settings.primary_llm
    if not provider.get("api_key"):
        raise ValueError("LLM API key 未配置，请检查 .env")

    start = time.perf_counter()

    with langfuse.start_as_current_observation(
        name=trace_name,
        as_type="generation",
        model=provider["model"],
        model_parameters={"temperature": 0.4, "tools": tools},
        input={"messages": messages},
    ) as gen:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "messages": messages,
                        "tools": tools,          # function calling schema
                        "temperature": 0.4,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            msg = data["choices"][0]["message"]
            usage = data.get("usage", {})

            gen.update(
                output={"message": msg},
                usage_details={
                    "input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                    "total": usage.get("total_tokens", 0),
                },
            )
            logger.info(
                "LLM 工具调用 model=%s tokens=%s tool_calls=%s",
                provider["model"],
                usage.get("total_tokens", 0),
                len(msg.get("tool_calls") or []),
            )
            return {"message": msg, "usage": usage}

        except Exception as e:
            gen.update(level="ERROR", status_message=str(e))
            logger.error("LLM 工具调用失败: %s", e)
            raise
