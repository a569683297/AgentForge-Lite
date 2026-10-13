"""
LLM Gateway（D7 版本：双通道降级 + Langfuse 追踪）
=====================================================
统一管理所有 LLM 调用，业务代码不得直连 SDK（架构红线）。

D5：单通道 + Langfuse 追踪
D7：双通道降级（主通道失败 → 自动切备用通道）
M6 优化②：新增流式通道（chat_with_tools_stream）—— 对话台 SSE 的底层出口。
    与非流式**并存**：非流式仍是评测 judge / 回归脚本的通道，两边独立演进。

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

S2（模型切换）：`model_id` 锁定 + `model_used` 归因
------------------------------------------------------------------
对话台右上角选择模型后，对话请求携带 `model_id`（内置通道名 / 自定义模型 UUID）。
`_resolve_channels` 新增 model_id 分支：解析为**单个**通道（锁定语义与 D22 一致 ——
失败就抛、不切备用）；解析失败（不存在 / 被禁用 / 内置未配 key）抛
`ModelResolutionError`，由路由层翻译成 422（非流式）或 error 帧（流式）。

网关返回值新增 `model_used`（**实际执行**的通道身份）：归因"本轮由谁回答"。
"用户选了什么"（意图）与"实际用了什么"（事实）都必须是数据 ——
§4.3 的"锁定不降级"保证两者在正常路径一致、在失败路径不撒谎。
"""

import json
import time
import uuid
from typing import Any, Callable

from langfuse import Langfuse

from app.config import LLM_CHANNELS, settings
from app.core.logging import logger
# S2：按 UUID 查自定义模型（llm_models 表）复用 model_service，不重复写 SQL。
# 无循环依赖：model_service 刻意不 import 本模块（连通性探测不走网关，见其 docstring）。
from app.services import model_service


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


class ModelResolutionError(Exception):
    """
    `model_id` 解析失败：不存在 / 已被禁用 / 内置通道未配 key（S2）。

    为什么单独一个异常类型（而不是 RuntimeError / ValueError 一把抓）：
    调用方要按类型分叉处置 ——
      · 路由层：非流式 → 422（**用户输入问题**，不是服务端故障）；
                流式 → error 帧（HTTP 200 已先行，只能用帧传达）；
      · 其余异常（网络 / 图崩溃）继续走既有的 500 / 通用 error 帧。
    靠异常类型分叉，比在路由里对错误信息做字符串匹配可靠得多。
    """


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

    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        # 自定义本地端点（如 Ollama）允许不带 key —— 与其带一个假的
        # "Bearer None"，不如不带（同 model_service._probe_chat_endpoint 的取舍）
        headers["Authorization"] = f"Bearer {provider['api_key']}"

    # S2：本机/内网端点（自定义模型如 Ollama）绕过环境代理，外网跟随 ——
    # 策略唯一出处 model_service.build_outbound_client（S1 只修了探测路径，
    # S2 把对话路径补齐：否则 host.docker.internal 这类域名会被代理短路成假错误）
    async with model_service.build_outbound_client(provider["base_url"], timeout=60) as client:
        resp = await client.post(
            f"{provider['base_url']}/chat/completions",
            headers=headers,
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
# 内部：单通道**流式**调用（M6 优化②；与 _call_once 同构）
# ============================================================
async def _call_once_stream(
    provider: dict,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    trace_name: str,
    gen: Any,
    temperature: float = DEFAULT_TEMPERATURE,
    on_content: Callable[[str], None] | None = None,
) -> dict:
    """
    用指定 provider **流式**调用一次 LLM，逐帧拼接出与非流式一致的结果。

    与非流式的三点结构差异（每一点都对着真实帧格式，2026-10-13 实测 DeepSeek 官方）：
      ① 请求体加 `stream: true` + `stream_options.include_usage: true` ——
         不加后者，末帧**不会带 usage**，Langfuse 的 token 统计会全部归零
         （OpenAI 协议的扩展参数，DeepSeek 官方已实测支持）。
      ② 响应是 SSE 帧（`data: {...}` 行、`data: [DONE]` 收尾），不是一整只 JSON ——
         用 httpx 流式接口逐行读，边读边回调。
      ③ `tool_calls[].function.arguments` 是**逐字符分片**到达的
         （实测原样：`{`、`"`、`city`、`": "`、`"北京"`、`"}`…），
         必须按 `index` 聚合并拼接；`id` / `name` 只在首片出现。

    返回值刻意与非流式**同构**（`{"data": ..., "usage": ...}`，message 字段一一对应）——
    这样"流式 vs 非流式"对上层只差"token 何时被看到"，落库/引用校验/工具判定零改动。

    Args:
        on_content: 每收到一段 content 增量就同步回调一次（**只对非空增量调用**）。
                    传 None 时等价于"只拼接、不回调"（可当非流式用）。
    """
    payload: dict[str, Any] = {
        "model": provider["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        # 让末帧带上 usage（否则 Langfuse 看不到 token）。
        # ⚠ 这是 OpenAI 协议的扩展参数：DeepSeek 官方支持；若某中转通道拒绝它，
        #   会在**连接期**（还没出字时）失败 → 走降级逻辑换通道，不会污染已出的内容。
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools

    start = time.perf_counter()
    content_parts: list[str] = []
    tool_slots: dict[int, dict] = {}   # index → {id, type, function:{name,arguments}}（分片聚合）
    usage: dict = {}
    finish_reason: str | None = None

    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        # 同 _call_once：自定义本地端点（Ollama）允许不带 Authorization
        headers["Authorization"] = f"Bearer {provider['api_key']}"

    # 同 _call_once：本机/内网端点绕过环境代理（策略唯一出处 build_outbound_client）
    async with model_service.build_outbound_client(provider["base_url"], timeout=60) as client:
        async with client.stream(
            "POST",
            f"{provider['base_url']}/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            resp.raise_for_status()   # 非 2xx 抛异常（触发降级，见 _call_with_failover_stream）
            async for line in resp.aiter_lines():
                # 只要 `data:` 行；空行、": keep-alive" 注释行一律跳过
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                chunk = json.loads(data_str)

                # usage 可能出现在任意帧：实测 DeepSeek 附在 finish 帧上；
                # OpenAI 系可能再加一帧"choices 为空、只有 usage"。—— 先于 choices 判断处理
                if chunk.get("usage"):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    if on_content is not None:
                        on_content(text)

                for tc in delta.get("tool_calls") or []:
                    slot = tool_slots.setdefault(
                        tc.get("index", 0),
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    # name / arguments 都按"分片拼接"处理：
                    # 实测正常情况下 name 只在首片完整出现（拼接与覆盖等价）；
                    # 但协议允许它被切片到达，拼接才是对所有通道都正确的写法。
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

    # 拼回与非流式同形的 assistant 消息
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_slots:
        message["tool_calls"] = [tool_slots[i] for i in sorted(tool_slots)]

    output: dict = {}
    if tools:
        output = {"message": message}
    else:
        output = {"reply": message["content"]}
    gen.update(
        output=output,
        usage_details={
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "total": usage.get("total_tokens", 0),
        },
    )
    logger.info(
        "LLM 流式调用成功 model=%s tokens=%s 耗时=%.2fs",
        provider["model"],
        usage.get("total_tokens", 0),
        time.perf_counter() - start,
    )
    return {
        "data": {"choices": [{"message": message, "finish_reason": finish_reason}]},
        "usage": usage,
    }


# ============================================================
# 通道选择（D22 锁定通道；S2 扩展 model_id）
# ============================================================
def _annotate(
    channel: dict, model_id: str, name: str, *, allow_empty_key: bool = False
) -> dict:
    """
    给通道附上内部标记（下划线开头 = 仅供本模块内部消费，不构成请求体）：

      `_model_id` / `_model_name`：S2 归因用 —— 最终成为响应里的 `model_used`；
      `_allow_empty_key`：自定义本地端点（如 Ollama）不需要认证 ——
        降级循环据此放行"没有 api_key"的通道（否则会被原规则静默跳过）。

    刻意返回**副本**而不是原地改：虽然 settings 的那些属性每次都返回新 dict，
    但"依赖对方每次给新对象"是隐式契约，不值得赌。
    """
    annotated = dict(channel)
    annotated["_model_id"] = model_id
    annotated["_model_name"] = name
    if allow_empty_key:
        annotated["_allow_empty_key"] = True
    return annotated


def _model_used_of(channel: dict) -> dict:
    """通道 → 响应的 `model_used`（归因三件套：id / 显示名 / 真实模型标识）。"""
    return {
        "id": channel.get("_model_id") or "",
        "name": channel.get("_model_name") or channel["model"],
        "model": channel["model"],
    }


def _builtin_label(name: str) -> str:
    """内置通道的显示名（与 /api/models 列表同一份映射，唯一出处见 model_service）。"""
    return model_service.BUILTIN_LABELS.get(name, name)


async def _resolve_locked_model(model_id: str) -> tuple[dict, str]:
    """
    `model_id` → 单个**锁定**通道（S2）。取值两种：

        · 内置通道名（"deepseek" / "openai"）→ 走 .env 配置，未配 key 直接报错；
        · 自定义模型 UUID → 查 llm_models 表（必须 enabled）。

    失败一律抛 `ModelResolutionError`（**绝不静默回退主通道**）：
    回退会让"我选的是 A"悄悄变成"实际用了 B"。而响应里的 `model_used`
    是要给用户看的归因 —— 归因不能是谎话，所以解析失败必须显式失败。

    自定义通道**允许没有 api_key**（Ollama 这类本地端点不需要认证），
    标记 `_allow_empty_key` 放行降级循环的原"无 key 即跳过"规则。
    """
    if model_id in LLM_CHANNELS:
        channel = settings.llm_channel(model_id)
        if not channel.get("api_key"):
            raise ModelResolutionError(
                f"内置模型 {model_id!r} 未配置 api_key，无法使用（检查 .env）"
            )
        label = _builtin_label(model_id)
        return _annotate(channel, model_id, label), f"选定模型({label})"

    try:
        custom_uuid = uuid.UUID(model_id)
    except ValueError:
        raise ModelResolutionError(
            f"未知的模型 id：{model_id!r}（既不是内置通道名，也不是自定义模型 UUID）"
        ) from None

    row = await model_service.get_model(custom_uuid)
    if row is None:
        raise ModelResolutionError(f"所选模型不存在（可能已被删除）：{model_id}")
    if not row.enabled:
        raise ModelResolutionError(f"所选模型已被禁用：{row.name}")

    channel = {
        "api_key": row.api_key,      # 允许为 None（本地端点不需要）
        "base_url": row.base_url,
        "model": row.model,
    }
    return (
        _annotate(channel, str(row.id), row.name, allow_empty_key=True),
        f"选定模型({row.name})",
    )


async def _resolve_channels(
    provider: str | None,
    allow_failover: bool,
    *,
    model_id: str | None = None,
) -> list[tuple[dict, str]]:
    """
    决定这次调用可以用哪些通道，返回 [(通道配置, 可读标签)]。

    四种组合，语义各不相同：

        model_id 指定（S2：用户在对话台选中的模型）
            → **锁定**：只返回该模型对应的一个通道，失败就抛错、绝不切备用。
              解析细节见 `_resolve_locked_model`。

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

    S2：返回的通道上都附有 `_model_id` / `_model_name`（见 `_annotate`）——
    降级循环把**实际成功的那一个**带进返回值，成为 `model_used`。
    这样"默认链路撞上降级（主→备）"时，归因记录的也是真实执行者，
    而不是"配置上本该用的那个"。
    """
    if model_id is not None:
        return [await _resolve_locked_model(model_id)]

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
        annotated = _annotate(channel, provider, _builtin_label(provider))
        return [(annotated, f"锁定通道({channel['model']})")]

    primary = settings.primary_llm
    channels: list[tuple[dict, str]] = [
        (
            _annotate(primary, settings.llm_provider, _builtin_label(settings.llm_provider)),
            "主通道",
        )
    ]
    if allow_failover:
        backup = settings.backup_llm
        if backup:
            # backup_llm 只可能在 llm_provider=deepseek 时存在（见 config 属性）→ 它就是 openai
            channels.append(
                (
                    _annotate(backup, "openai", _builtin_label("openai")),
                    f"备用通道({backup['model']})",
                )
            )
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
    model_id: str | None = None,
) -> dict:
    """带降级的调用。返回 {"data": ..., "usage": ..., "provider": 通道标签, "model_used": 实际通道身份}。"""
    channels = await _resolve_channels(provider, allow_failover, model_id=model_id)

    last_error: Exception | None = None

    for idx, (channel, provider_label) in enumerate(channels):
        # 无 key 的通道默认跳过；但自定义本地端点（Ollama）经 _resolve_channels
        # 带上 `_allow_empty_key` 标记 —— 它们本来就不需要认证，必须放行。
        if not channel.get("api_key") and not channel.get("_allow_empty_key"):
            continue

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
                # S2：把**实际成功**的通道身份带出去（默认链路降级到备用时，
                # 记录的也是真实执行者 —— 归因不能是"配置上本应是谁"）
                result["model_used"] = _model_used_of(channel)
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
    if provider is not None or model_id is not None:
        # 锁定模式（provider=judge / model_id=用户选定）**不包装异常**：
        # 包装会把 httpx.HTTPStatusError 变成 RuntimeError，
        # 调用方（judge 的重试逻辑、路由层的错误翻译）就没法判断
        # "这个错重试有没有意义" —— 429 该重试、401 不该，
        # 而两者包成 RuntimeError 后长得一模一样。信息在包装时被丢掉了，这比不包装更糟。
        raise last_error
    raise RuntimeError(f"所有 LLM 通道均失败: {last_error}") from last_error


# ============================================================
# 降级循环（流式版）：与 _call_with_failover 只差一条"出字即锁"
# ============================================================
async def _call_with_failover_stream(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    trace_name: str,
    temperature: float = DEFAULT_TEMPERATURE,
    on_content: Callable[[str], None] | None = None,
    model_id: str | None = None,
) -> dict:
    """
    带降级的**流式**调用：主通道 → 失败 → 备用通道 → 失败 → 抛错。

    与非流式降级唯一的语义差异 —— **"已出字"就不再换通道**：

        非流式：失败 = 什么都没发生 → 换备用通道重来，用户全程无感。
        流式：  失败可能发生在**已经吐出一截答案之后**。此时换通道重来，
                用户屏幕上会变成"半截话 + 另一个模型从头再讲一遍" ——
                两段文字拼在一起，看起来像模型胡言乱语，比直接报错更糟。
                正确规则：**一个字都没出的失败才允许降级；出字之后再失败，直接抛**。

    这条差异只在流式里存在 —— 因为流式把"过程"发给了用户，而发出去的东西收不回来。

    S2：新增 `model_id` —— 用户在对话台选了模型时同样走锁定
    （用户明确选定的模型失败后绝不允许静默换通道：响应里的归因必须是真的）。
    与 D22 的 provider 锁定共用 `_resolve_channels` 的锁定分支。
    """
    channels = await _resolve_channels(None, True, model_id=model_id)

    last_error: Exception | None = None

    for idx, (channel, provider_label) in enumerate(channels):
        # 同 _call_with_failover：自定义本地端点带 `_allow_empty_key`，不参与跳过
        if not channel.get("api_key") and not channel.get("_allow_empty_key"):
            continue

        # "这条通道有没有已经吐过字" —— 决定失败后能否安全降级
        emitted = False

        def _emit(text: str) -> None:
            nonlocal emitted
            emitted = True
            if on_content is not None:
                on_content(text)

        with langfuse.start_as_current_observation(
            name=trace_name,
            as_type="generation",
            model=channel["model"],
            model_parameters={"temperature": temperature, **({"tools": tools} if tools else {})},
            input={"messages": messages},
            metadata={"provider": provider_label, "stream": True},
        ) as gen:
            try:
                result = await _call_once_stream(
                    channel,
                    messages,
                    tools=tools,
                    trace_name=trace_name,
                    gen=gen,
                    temperature=temperature,
                    on_content=_emit,
                )
                result["provider"] = provider_label
                result["model_used"] = _model_used_of(channel)   # S2 归因（同非流式）
                return result
            except Exception as e:
                last_error = e
                gen.update(level="ERROR", status_message=f"{provider_label} 失败: {e}")
                if emitted:
                    # 已经吐过字 —— 不能换通道（理由见 docstring），原样抛出。
                    # 不包装：让调用方看到真实异常（与 D22 锁定模式不包装同一条理由）。
                    logger.warning("%s 流式调用中途失败（已输出内容，不再降级）", provider_label)
                    raise
                logger.warning(
                    "%s 流式调用失败（尚未输出内容），%s",
                    provider_label,
                    "切换备用通道" if idx < len(channels) - 1 else "无可用通道",
                )

    assert last_error is not None, "通道列表非空却没有任何异常，说明 _resolve_channels 返回了空列表"
    if model_id is not None:
        # 选定模型失败：与"已出字"分支同一取舍 —— 不包装，让调用方看到真实异常
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
    model_id: str | None = None,
) -> dict:
    """发送对话到 LLM，支持 function calling（默认自动降级）。

    S2：新增 `model_id`（内置通道名 / 自定义 UUID）→ 锁定所选模型、失败不降级；
        解析失败抛 `ModelResolutionError`。不传 = 原有行为逐字不变（评测零影响）。

    Returns:
        {"message": {role, content, tool_calls?}, "usage": {...},
         "model_used": 实际执行通道身份（S2 归因用）}
    """
    result = await _call_with_failover(
        messages,
        tools=tools,
        trace_name=trace_name,
        temperature=temperature,
        provider=provider,
        allow_failover=allow_failover,
        model_id=model_id,
    )
    return {
        "message": result["data"]["choices"][0]["message"],
        "usage": result["usage"],
        "model_used": result.get("model_used"),
    }


async def chat_with_tools_stream(
    messages: list[dict],
    tools: list[dict],
    *,
    trace_name: str = "llm-tools",
    temperature: float = DEFAULT_TEMPERATURE,
    on_content: Callable[[str], None] | None = None,
    model_id: str | None = None,
) -> dict:
    """发送对话到 LLM（**流式**，默认自动降级），边收边通过 `on_content` 回调吐字。

    与非流式 `chat_with_tools` 的关系：
      - 入参、返回值**同构**（`{"message": ..., "usage": ..., "model_used": ...}`）；
      - 唯一新增参数 `on_content`：每收到一段 content 增量就同步回调一次；
      - 失败降级只在"一个字都没吐"时生效（见 `_call_with_failover_stream`）；
      - S2：`model_id` 与 `chat_with_tools` 同义（锁定所选模型，失败不降级）。

    调用方（plan_node）负责把 `on_content` 接到下游出口（LangGraph 的 stream writer）——
    Gateway 不知道 SSE / 事件协议的存在，这一层只负责"把流转出来"。
    """
    result = await _call_with_failover_stream(
        messages,
        tools=tools,
        trace_name=trace_name,
        temperature=temperature,
        on_content=on_content,
        model_id=model_id,
    )
    return {
        "message": result["data"]["choices"][0]["message"],
        "usage": result["usage"],
        "model_used": result.get("model_used"),
    }
