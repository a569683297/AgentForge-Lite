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

D22：新增**锁定通道**能力（`provider=...`）
------------------------------------------------------------------
背景：评测的 judge 不能走自动降级。降级的语义是"主通道失败 → 静默切备用"，
judge 若走它，会出现"一部分样本是 deepseek 打的、一部分是中转 GPT 打的"，
而报告上只写一个 judge 名字 → 分数不可比、且没有任何报错。

而 PRD §8.2 的架构红线又要求"业务代码不得绕过 Gateway 直连 SDK"
（绕了就没有 Langfuse 追踪、没有 token 统计）。D20 的探针脚本当时不得不绕过，
D22 改为**给 Gateway 加锁定通道的能力**，而不是让调用方自己发 httpx。

    chat(..., provider="deepseek")   → 只用该通道，失败就抛，不偷偷换
    chat(..., allow_failover=False)  → 不指定通道时，只用主通道

这是"降级是有代价的"这条认识的具体落地：**降级让"用了哪个通道"这件事从数据里消失**。
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


# ---- 采样温度 ----
# PRD §8.4「场景参数」：规划 temp=0.2、回答 temp=0.4、摘要 temp=0.1、评测 judge temp=0。
# D14 把 temperature 从硬编码改成可传参数 —— 不改的话摘要会被迫用 0.4 这个
# "创作档"去压缩历史，容易加戏、改事实；摘要本身要的是"忠实压缩"，所以要用 0.1。
DEFAULT_TEMPERATURE = 0.4


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
    temperature: float = DEFAULT_TEMPERATURE,
) -> dict:
    """
    用指定 provider 调用一次 LLM，返回解析后的响应数据。

    Args:
        provider: {"api_key", "base_url", "model"} 通道配置
        tools: 传了就走 function calling，否则普通 chat
        gen: Langfuse observation（由调用方创建，这里只负责 update）
        temperature: 采样温度，默认走 DEFAULT_TEMPERATURE（回答场景）
    Returns:
        {"reply": str} 或 {"message": dict}（取决于是否带 tools）
    """
    start = time.perf_counter()
    payload: dict[str, Any] = {
        "model": provider["model"],
        "messages": messages,
        "temperature": temperature,
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
# 通道选择（D22）
# ============================================================
def _resolve_channels(
    provider: str | None,
    allow_failover: bool,
) -> list[tuple[dict, str]]:
    """
    决定这次调用可以用哪些通道，返回 [(通道配置, 可读标签)]。

    三种组合，语义各不相同：

        provider 指定（如 "deepseek"）
            → **锁定**：只返回这一个通道，`allow_failover` 被忽略。
              这是评测 judge 用的模式 —— 失败就抛错，绝不偷偷换模型。
              忽略 allow_failover 是刻意的：既然指定了通道，就不是"优先用它"
              而是"必须是它"；否则"锁定"这个词没有意义。

        provider=None 且 allow_failover=True
            → 主通道 + 备用通道（原有行为，业务对话默认走这条）

        provider=None 且 allow_failover=False
            → 只用主通道

    标签在这里统一生成（而不是让 `_call_with_failover` 按下标猜）：
    锁定模式下"第 0 个"不是主通道，按下标命名会写出
    "主通道(模型X)" 这种与事实不符的日志，而日志正是排查降级问题的唯一线索。
    """
    if provider is not None:
        channel = settings.llm_channel(provider)      # 非法名字在这里抛错
        if not channel.get("api_key"):
            # 必须显式报错。若沿用下面的 `continue` 逻辑，会走到"所有通道均失败"
            # 而 `last_error` 是 None → 报出 "所有 LLM 通道均失败: None"，
            # 真正的病因（这个通道没配 key）被完全埋掉。
            raise RuntimeError(
                f"指定的 LLM 通道 {provider!r} 没有配置 api_key"
                f"（model={channel.get('model')!r}）"
            )
        return [(channel, f"锁定通道({channel['model']})")]

    channels: list[tuple[dict, str]] = [(settings.primary_llm, "主通道")]
    if allow_failover:
        backup = settings.backup_llm
        if backup:
            channels.append((backup, f"备用通道({backup['model']})"))
    return channels


# ============================================================
# 降级循环：主通道 → 失败 → 备用通道 → 失败 → 抛错
# ============================================================
async def _call_with_failover(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    trace_name: str,
    temperature: float = DEFAULT_TEMPERATURE,
    provider: str | None = None,
    allow_failover: bool = True,
) -> dict:
    """带降级的调用。返回 {"data": ..., "usage": ..., "provider": 实际使用的通道}。"""
    channels = _resolve_channels(provider, allow_failover)

    last_error: Exception | None = None

    for idx, (channel, provider_label) in enumerate(channels):
        if not channel.get("api_key"):
            continue  # 该通道无 key，跳过（锁定模式的缺 key 已在 _resolve_channels 拦掉）

        # 每个通道独立创建 observation（Langfuse 能看到哪个通道被调用）
        with langfuse.start_as_current_observation(
            name=trace_name,
            as_type="generation",
            model=channel["model"],
            model_parameters={"temperature": temperature, **({"tools": tools} if tools else {})},
            input={"messages": messages},
            metadata={"provider": provider_label},
        ) as gen:
            try:
                result = await _call_once(
                    channel,
                    messages,
                    tools=tools,
                    trace_name=trace_name,
                    gen=gen,
                    temperature=temperature,
                )
                result["provider"] = provider_label
                return result
            except Exception as e:
                last_error = e
                gen.update(level="ERROR", status_message=f"{provider_label} 失败: {e}")
                logger.warning(
                    "%s 调用失败，%s",
                    provider_label,
                    "切换备用通道" if idx < len(channels) - 1 else "无可用通道",
                )

    # 所有通道都失败（锁定模式下就是"该通道失败"，措辞要能区分这两种情形）
    assert last_error is not None, "通道列表非空却没有任何异常，说明 _resolve_channels 返回了空列表"
    if provider is not None:
        # 锁定模式**不包装异常**：包装会把 httpx.HTTPStatusError 变成 RuntimeError，
        # 调用方（judge 的重试逻辑）就没法判断"这个错重试有没有意义" ——
        # 429 该重试、401 不该，而两者包成 RuntimeError 后长得一模一样。
        # 信息在包装时被丢掉了，这比不包装更糟。
        raise last_error
    raise RuntimeError(f"所有 LLM 通道均失败: {last_error}") from last_error


# ============================================================
# 对外 API
# ============================================================
# 签名新增的两个参数都有默认值 → 已有调用方零改动（原行为完全不变）。
async def chat(
    messages: list[dict],
    *,
    trace_name: str = "llm-chat",
    user_id: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    provider: str | None = None,
    allow_failover: bool = True,
) -> str:
    """发送对话到 LLM（默认自动降级）。返回回复文本。

    D14：新增 temperature（默认 0.4 = 回答场景），调用方不传即旧行为。
    D22：新增 provider / allow_failover —— 评测 judge 用它们**锁定通道**，
         见 `_resolve_channels` 的说明。
    """
    result = await _call_with_failover(
        messages,
        trace_name=trace_name,
        temperature=temperature,
        provider=provider,
        allow_failover=allow_failover,
    )
    return result["data"]["choices"][0]["message"]["content"]


async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    trace_name: str = "llm-tools",
    temperature: float = DEFAULT_TEMPERATURE,
    provider: str | None = None,
    allow_failover: bool = True,
) -> dict:
    """发送对话到 LLM，支持 function calling（默认自动降级）。

    Returns:
        {"message": {role, content, tool_calls?}, "usage": {...}}
    """
    result = await _call_with_failover(
        messages,
        tools=tools,
        trace_name=trace_name,
        temperature=temperature,
        provider=provider,
        allow_failover=allow_failover,
    )
    return {"message": result["data"]["choices"][0]["message"], "usage": result["usage"]}
