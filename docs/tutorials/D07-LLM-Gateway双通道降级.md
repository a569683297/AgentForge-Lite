# D7 复习教程：LLM Gateway 双通道降级

> 日期：2026-09-01 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过（三项验证）

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. 为什么需要双通道？（两个理由）
2. 降级机制（failover）怎么工作？
3. "主通道失败"具体指什么情况？
4. 为什么换通道这么容易？（OpenAI 兼容协议）
5. 为什么改 Gateway 就能让全项目获得降级能力？

---

## 2. 核心概念讲解

### 2.1 为什么需要双通道

**理由 1：单点故障（Single Point of Failure）**
- LLM 供应商经常出问题（限流 429、超时、维护、余额不足）
- 只依赖一家 → 它一挂，整个应用不能回答任何问题
- 类比：公司只用一个云服务商，它一挂全公司瘫痪

**理由 2：供应商锁定（Vendor Lock-in）**
- 代码到处直连 SDK → 换/加供应商要改所有地方
- Gateway 统一入口 + 配置化 → 换供应商只改 .env
- 类比：多云架构

### 2.2 降级机制（Failover）

```
请求 → chat()
  → 主通道（DeepSeek）
    → 成功 → 返回 ✅
    → 失败（异常/超时/非2xx）→ 记日志
  → 备用通道（OpenAI）
    → 成功 → 返回 ✅（日志标记"已降级"）
    → 失败 → 两个都挂了 → 抛异常
```

**关键设计**：
1. 主通道失败才切（平时完全走主，便宜/快）
2. 切换要留痕（日志 + Langfuse metadata 标 provider）
3. 两个都失败才抛错（不因单一供应商故障导致请求失败）

**类比**：汽车备胎——主胎爆了换备胎，备胎也爆了才叫救援。

### 2.3 为什么换通道容易（OpenAI 兼容协议）

DeepSeek 和 OpenAI 的 API **协议几乎一样**（都是 chat completions 格式）：
```
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
{"model": ..., "messages": [...], "tools": [...]}
```
换通道只需换三样：`base_url` / `api_key` / `model`——**消息格式不用改**。

**D1 前瞻性**：config.py 从第一天就设计了 `primary_llm` / `backup_llm` 属性，今天只是用起来。

### 2.4 统一入口的架构红利（第二次体现）

```
业务代码 → llm_gateway.chat()（今天加降级）
              ↓
         _call_with_failover() → 主 → 失败 → 备
```
- D5：埋点写一次，所有调用方免费获得追踪
- D7：**降级写一次，所有调用方免费获得高可用**
- Agent 引擎一行代码没改就自动获得降级（今天验证 ③）

---

## 3. 代码逐行解读

### 3.1 _call_once（单通道调用，主备共用）

```python
async def _call_once(provider, messages, *, tools=None, trace_name, gen) -> dict:
    payload = {"model": provider["model"], "messages": messages, "temperature": 0.4}
    if tools:
        payload["tools"] = tools
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{provider['base_url']}/chat/completions", ...)
        resp.raise_for_status()   # 非 2xx 抛异常 → 触发降级
        data = resp.json()
    gen.update(output=..., usage_details=...)   # Langfuse 记录
    return {"data": data, "usage": usage}
```
> **只干一件事**：用指定 provider 调一次 LLM + 记录。主备通道共用。
> `raise_for_status()` 是降级的触发点——任何非 2xx（400/401/429/5xx）都抛异常。

### 3.2 _call_with_failover（降级循环）

```python
async def _call_with_failover(messages, *, tools=None, trace_name):
    providers = [settings.primary_llm]
    backup = settings.backup_llm
    if backup:
        providers.append(backup)

    for idx, provider in enumerate(providers):
        if not provider.get("api_key"):
            continue
        with langfuse.start_as_current_observation(...) as gen:   # 每通道独立 observation
            try:
                result = await _call_once(provider, messages, tools=tools, trace_name=trace_name, gen=gen)
                result["provider"] = f"主通道" if idx == 0 else f"备用通道"
                return result
            except Exception as e:
                gen.update(level="ERROR", ...)
                logger.warning("主通道 调用失败，切换备用通道")
    raise RuntimeError(f"所有 LLM 通道均失败: {last_error}")
```
> 遍历主→备；失败记日志换下一个；全挂才抛错。
> **每通道独立 observation**：Langfuse 能看到"主失败→备成功"的全过程（metadata 标 provider）。

### 3.3 对外 API（签名不变）

```python
async def chat(messages, *, trace_name="llm-chat", user_id=None) -> str:
    result = await _call_with_failover(messages, trace_name=trace_name)
    return result["data"]["choices"][0]["message"]["content"]
```
> **调用方零改动**——Agent 引擎（D6）自动获得降级。这是重构的关键原则：内部改实现，外部签名稳定。

---

## 4. 类比迁移表

| 概念 | 类比 |
|---|---|
| 双通道 | 多云架构 / 备胎 |
| 主通道失败才切 | 主胎爆了才换备胎 |
| OpenAI 兼容协议 | 统一的插座标准（不同品牌电器同一插口） |
| _call_once 抽取 | DRY：公共函数避免重复 |
| 对外签名不变 | 接口稳定：内部重构不破坏调用方 |

---

## 5. 面试考点

**Q1：你的 LLM 集成怎么做的？**
→ 统一 LLM Gateway 入口，双通道（DeepSeek 主/OpenAI 备），主通道失败自动降级到备用，两个都失败才报错。所有业务代码走 Gateway，不直连 SDK。

**Q2：为什么做双通道？**
→ ① 单点故障：LLM 供应商常出问题（限流/超时），不能依赖单家 ② 供应商锁定：Gateway 配置化切换，换供应商只改 .env。

**Q3：什么情况触发降级？**
→ 任何异常：网络超时、非 2xx（400/401/429/5xx）、连接错误。`raise_for_status()` 抛异常即触发。

**Q4：降级对用户有什么影响？**
→ 无感知：主通道失败自动切备，用户正常收到回答。只在两个通道都挂时才失败（极少见）。

---

## 6. 自测题（附参考答案，先自己做再看）

1. 为什么需要双通道？（两个理由）
   **参考答案**：① 单点故障——LLM 供应商常出问题，不能依赖单家 ② 供应商锁定——Gateway 配置化切换，换供应商只改 .env。

2. 降级机制怎么工作？
   **参考答案**：先试主通道，失败记日志切备用，备用也失败才抛错。两个通道都挂了才导致请求失败。

3. "主通道失败"具体指什么？
   **参考答案**：任何异常——网络超时、非 2xx（400/401/429/5xx）、连接错误。由 `raise_for_status()` 触发。

4. 为什么换通道这么容易？
   **参考答案**：OpenAI 兼容协议——DeepSeek/OpenAI 的 chat completions 格式几乎一样，换通道只需换 base_url/api_key/model，消息格式不用改。

5. 为什么改 Gateway 就能让全项目获得降级？
   **参考答案**：所有业务代码统一走 Gateway 入口，降级逻辑写在 Gateway 内部，调用方（Agent 引擎/API）无需改动自动受益。这是统一入口的架构红利。

---

## 7. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| （今日无新坑） | 三项验证一次通过 | 主/备/降级/Agent 全链路 ✅ |
| 注意 | 备用模型名非标准（gpt-5.6-sol） | 已验证真实可用（调用成功返回），Langfuse 面板可确认 model 字段 |

---

*D7 完 ｜ 下一篇：D8 Tool Registry + 记忆滑窗（5 轮上下文正确）*
