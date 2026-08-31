# D5 复习教程：Langfuse 可观测与 LLM Gateway

> 日期：2026-08-31 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过（面板确认 trace）

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. 可观测性是什么？三支柱（日志/指标/追踪）各回答什么问题？
2. 为什么 LLM 应用比普通后端更需要"追踪"？
3. Trace 和 Span 是什么关系？
4. LLM Gateway 是干什么的？为什么业务代码不能直连 SDK？
5. Langfuse v4 SDK 的核心用法（上下文管理器）？

---

## 2. 核心概念讲解

### 2.1 可观测性（Observability）

**一句话**：可观测性 = 让系统内部状态"看得见"，出问题时能回答"发生了什么、在哪、为什么"。

**三支柱**（业界标准，面试必背）：

| 支柱 | 回答的问题 | 类比 |
|---|---|---|
| 日志 Logs | 发生了什么（逐条事件） | 案件记录本 |
| 指标 Metrics | 有多少/多快（计数/延迟） | 仪表盘数字 |
| 追踪 Traces | 一次请求完整经过哪些步骤、每步多久 | 快递单全程追踪 |

**你的项目现状**：日志 ✅（D1 有 logging）、指标 ❌、追踪 ✅（今天补上）

### 2.2 为什么 LLM 应用特别需要追踪

**普通后端**：请求 → 路由 → 查库 → 返回，每一步是确定性代码，错了有堆栈，日志够用。

**LLM 应用**：一次对话 = **多次 LLM 调用**（规划/工具/回答），每次都是"概率性的"（可能答错/超时/幻觉）：
```
用户提问 → Agent 思考(LLM#1) → 调工具 → 再思考(LLM#2) → RAG 检索 → 回答(LLM#3)
```
**问题**：回答不准时，是哪一步的 prompt 错了？工具返回错了？检索漏了？**没有追踪 = 无法定位**。这就是 Langfuse 存在的意义。

### 2.3 Trace 和 Span（核心概念）

```
Trace（一次请求/对话）                    ← 最外层（快递单号）
├── Span: LLM 调用 #1（generation）       ← 子步骤（记录 model/token/耗时）
├── Span: 工具调用                        ← 子步骤
├── Span: RAG 检索                        ← 子步骤
└── Span: LLM 调用 #2（generation）       ← 子步骤
```

- **Trace** = 一次请求的完整生命周期（类比：一次 HTTP 请求）
- **Span** = 一个子步骤的时间片段（类比：每个中间件的时间）
- **Langfuse 是专为 LLM 打造的可观测平台**：原生理解 token/模型/参数，还能算成本

### 2.4 LLM Gateway（架构红线）

```python
# app/services/llm_gateway.py —— 所有 LLM 调用唯一入口
业务代码 → llm_gateway.chat(messages) → 调 LLM → 返回
```

**为什么必须统一入口（面试重点）**：
1. **埋点集中**：追踪/日志/token 统计只写一次，全项目生效
2. **降级/切换**：D7 加 OpenAI 备用通道时，只改 gateway，业务代码零改动
3. **可控性**：可以统一加超时、重试、安全过滤（阶段 3 扩展点）

**类比**：前端封装的 axios 实例——所有请求走它，统一加 header/拦截器，不用每个页面自己 fetch。

### 2.5 Langfuse v4 SDK 用法（今天的实战版本）

```python
with langfuse.start_as_current_observation(
    name="llm-chat",        # 观察点名称
    as_type="generation",   # 类型：LLM 生成调用（专有类型）
    model=...,              # 模型名（面板显示）
    input={...},            # 输入（prompt）
) as gen:                   # 进入 with = 自动开始
    # ... 真实调用 LLM ...
    gen.update(output=..., usage_details={...})   # 记录输出 + token
    # 退出 with = 自动 end（不用手动调用）
```

**关键认知**：
- v4 用**上下文管理器**（with 块），进入自动开始、退出自动结束——类比 React useEffect 自动清理
- `as_type="generation"` = 标记"这是 LLM 生成调用"（还有 span/tool/retriever 等类型）
- 异步上报：SDK 后台批量发送，不阻塞你的请求（性能无损）

---

## 3. 代码逐行解读（按"必须懂 / 可略过"分层）

### 3.1 文件全景

```python
langfuse = Langfuse(
    public_key=settings.langfuse_public_key,   # 从 .env 读（Key 已配置）
    secret_key=settings.langfuse_secret_key,   # 从 .env 读
    host=settings.langfuse_host,               # 从 .env 读（Cloud）
)
```
> **必须懂**：Langfuse 客户端单例。key 从 config 读（D1 学的配置中心）。
> 注意：v4 是懒连接，创建对象不连接，第一次上报才连。

```python
async def chat(messages, *, trace_name="llm-chat", user_id=None) -> str:
```
> **必须懂**：函数签名。`*` 之后是关键字参数（调用时必须写参数名）。

```python
provider = settings.primary_llm   # 从 config 拿当前主通道配置
if not provider.get("api_key"):
    raise ValueError(...)
```
> **必须懂**：先校验配置再干活（快速失败）。D7 会扩展成双通道。

```python
with langfuse.start_as_current_observation(
    name=trace_name, as_type="generation", model=provider["model"],
    model_parameters={"temperature": 0.4}, input={"messages": messages},
    metadata={"user_id": user_id} if user_id else None,
) as gen:
```
> **必须懂**：上下文管理器 = 自动开始/结束。**可略过**：具体参数名（用的时候查文档）。

```python
async with httpx.AsyncClient(timeout=60) as client:
    resp = await client.post(f"{provider['base_url']}/chat/completions", ...)
```
> **可略过**：就是 axios/fetch 调 POST 接口（你熟），timeout=60 防卡死。

```python
reply = data["choices"][0]["message"]["content"]   # 取回复文本
usage = data.get("usage", {})                       # 取 token 统计
```
> **必须懂**：DeepSeek 返回格式（OpenAI 兼容协议）——`choices[0].message.content` 是回复，`usage` 是 token 数。

```python
gen.update(
    output={"reply": reply},
    usage_details={"input": ..., "output": ..., "total": ...},
)
```
> **必须懂**：把输出和 token 记录到 Langfuse（面板显示的关键）。**可略过**：方法名（v4 是 update）。

```python
except Exception as e:
    gen.update(level="ERROR", status_message=str(e))
    raise
```
> **必须懂**：失败也留痕（面板能看到失败调用）+ 重新抛出（调用方知道失败）。D4 优雅降级思想的延续。

---

## 4. 类比迁移表

| Langfuse/新概念 | 你已会的（前端） |
|---|---|
| Trace | 一次 HTTP 请求的完整链路 |
| Span | 每个中间件/函数的时间片段 |
| 上下文管理器（with） | React useEffect 自动清理 |
| 异步上报（不阻塞） | Web Worker / 后台任务 |
| LLM Gateway | 封装的 axios 实例（统一拦截器） |
| as_type="generation" | 日志分类 tag |

---

## 5. 面试考点

**Q1：可观测性是什么？你项目怎么做？**
→ 让系统内部状态可见。三支柱：日志（发生了什么）、指标（多少/多快）、追踪（完整链路）。我项目用结构化日志 + Langfuse 追踪（LLM 调用/工具/检索全埋点，记录 token/耗时/输入输出）。

**Q2：为什么 LLM 应用需要追踪而普通后端不需要？**
→ 普通后端每一步是确定性代码，错了有堆栈；LLM 应用一次对话多次模型调用，每次都是概率性的，回答不准时只有追踪能定位是哪一步（prompt/工具/检索）出问题。

**Q3：为什么所有 LLM 调用要统一走 Gateway？**
→ 埋点集中（追踪/日志/统计写一次全生效）、便于降级切换（D7 双通道只改 gateway）、可控（超时/重试/安全统一加）。

**Q4：Langfuse 异步上报会不会影响性能？**
→ 不会。SDK 后台队列批量发送，请求返回不等待上报完成，用户无感知。

**Q5：Trace 和 Span 的区别？**
→ Trace 是一次请求的完整生命周期（顶层），Span 是其中一个子步骤（LLM 调用/工具/检索）。一个 Trace 多个 Span，树状结构。

---

## 6. 自测题（附参考答案，先自己做再看）

1. 可观测性三支柱各回答什么问题？
   **参考答案**：日志=发生了什么；指标=有多少/多快；追踪=一次请求完整经过哪些步骤、每步多久。

2. 为什么 LLM 应用比普通后端更需要追踪？
   **参考答案**：普通后端是确定性代码，错了有堆栈；LLM 一次对话多次模型调用，每次概率性，回答不准时只有追踪能定位是哪一步（prompt/工具/检索）出问题。

3. Trace 和 Span 是什么关系？
   **参考答案**：Trace 是顶层（一次请求），Span 是子步骤（一次 LLM 调用/工具/检索）。一个 Trace 含多个 Span，树状。

4. Langfuse v4 的 `with ... as gen:` 块里，进入和退出分别发生什么？
   **参考答案**：进入 with = 自动开始 observation（创建 span），退出 = 自动 end（上报）。类比 useEffect 自动清理，不用手动调用开始/结束。

5. 为什么所有 LLM 调用要走 LLM Gateway 而不是业务代码直连 SDK？
   **参考答案**：埋点集中（一次写全项目生效）、降级切换方便（D7 双通道只改 gateway）、可控（统一超时/重试/安全）。类比前端统一封装的 axios。

---

## 7. 踩坑记录（今天 3 个坑）

| 坑 | 现象 | 解决 |
|---|---|---|
| **Langfuse v4 API 变化** | `langfuse.trace()` / `set_level()` 不存在 → AttributeError | 查 SDK 源码确认 v4 用 `start_as_current_observation`（上下文管理器）+ `gen.update()`。**教训：第三方 SDK 版本升级会改 API，遇到 AttributeError 先查版本和源码，不猜** |
| **host 配置名不匹配** | `.env` 写 `LANGFUSE_BASE_URL`，config 读 `LANGFUSE_HOST` → 配置没生效 | 改名对齐。**教训：靠默认值"碰巧工作"是隐性 bug，配置键名必须全项目一致** |
| **user_id 参数位置** | v4 observation 不接受 user_id 参数 → TypeError | v4 无直接 user 参数，改用 metadata 传。**教训：API 变化时先看函数签名再改** |

---

*D5 完 ｜ 下一篇：D6 学 LangGraph 状态机（重点学习日）*
