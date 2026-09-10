# D8 复习教程：Tool Registry 注册表 + 记忆滑窗

> 日期：2026-09-10 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过（注册表 + 多轮记忆 + 滑窗裁剪）

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. Tool Registry 解决什么问题？加新工具的流程变化？
2. 装饰器（`@register`）是怎么把函数注册进去的？执行时机？
3. 注册表的三个能力（register / get_tools_schema / execute_tool）各干什么？
4. 记忆滑窗解决什么问题？为什么用 Redis？
5. 为什么记忆必须 Upsert 不能 Append？
6. 记忆为什么不存 tool_calls / tool 消息？

---

## 2. 核心概念讲解

### 2.1 Tool Registry（工具注册表）

**解决什么**：D6 时代工具定义内联在 `agent_service.py` → 加工具要改 Agent 引擎（耦合）。

**方案**：中心化注册表 + 分散的工具定义
```
工具文件（@register 自己登记）→ 注册表 _registry → Agent 引擎查询（不关心有什么工具）
```

**加新工具的成本变化**：
| | D6 | D8 |
|---|---|---|
| 步骤 | 改 agent_service.py 的 TOOLS dict | ① 新建工具文件 ② 加 @register ③ __init__.py import 一行 |
| 是否碰引擎 | ✅ 要碰 | ❌ 零改动 |

**类比**：Vite 插件系统 / NestJS `@Injectable()` / Vue 全局组件注册——声明式注册，框架遍历。

### 2.2 装饰器执行机制（今天重点补齐）

**`@register(...)` 展开写法**：
```python
@register(name="current_time", description="...")
def current_time(): ...

# 完全等价于：
def current_time(): ...
current_time = register(name="current_time", description="...")(current_time)
#              └─ ① 调外层拿装饰器 ─┘  └─ ② 把函数传进去 ─┘
```

**三步**：
1. `register(name=..., description=...)` 被调用 → **返回内层 `decorator` 函数**（内层代码还没执行）
2. Python 把下面定义的函数当参数传给 `decorator` → **`_registry[name] = {...}` 执行（注册发生在这里）**
3. `decorator` 返回原函数 → 名字仍指向原函数（所以还能直接调用）

**执行时机**：**模块被导入时立即执行**（不是调用函数时）→ 所以 `__init__.py` 必须 import（副作用导入）

**命名自由度**：外层 `register`、内层 `decorator`、参数 `func` 都可随便改名；**固定的是两层嵌套结构 + 内层必须返回函数**。改名注意：外层是"对外 API"（改名要同步改 import），内层是"内部实现"（改名零成本）。

### 2.3 记忆滑窗

**解决什么**：Agent 无状态 → 多轮对话丢上下文（"我叫小王" → "我叫什么"答不出）。

**方案**：
```
用户提问（带 session_id）
  → ① 从 Redis 读最近 N 轮历史
  → ② 拼 messages = 历史 + [本次提问]
  → ③ 跑 Agent
  → ④ 取最终回答
  → ⑤ 写回 Redis（Upsert + 裁剪 + 刷新 TTL）
```

**为什么用 Redis**：对话窗口是热数据（快）+ TTL 自动过期（不用手动清理）+ 全量冷数据归 PG。

**滑窗**：只留最近 8 轮（16 条），新的进来旧的挤出去——防止 token 无限膨胀。

### 2.4 ⚠️ 两个真实工程坑

**坑 1：记忆必须 Upsert，不能 Append**
```python
# ❌ rpush（Append）：重试会写重复；单向无法裁剪
# ✅ 读→合并→裁剪→整体写回（Upsert）
window = await get_window(sid)
window += [user_msg, assistant_msg]
window = window[-16:]              # 裁剪
await redis.set(key, json.dumps(window), ex=86400)
```
**为什么**：① 重试/重放会写入重复消息 ② append 单向，删不掉最旧的。

**坑 2：记忆只存 user + 最终回答，不存 tool_calls / tool 消息**
**原因**：function calling 协议要求 tool_calls 与 tool 消息**成对出现**；只存一半 → 下一轮 400。
**决策**：记忆层只保留"对话语义"，工具调用细节留给单轮内处理。好处：避免 400 + 省 token + 语义清晰。

---

## 3. 代码逐行解读

### 3.1 app/tools/registry.py（注册表核心）

```python
_registry: dict[str, dict] = {}     # 模块级字典 = 天然单例（_ 前缀表示私有）
```

```python
def register(name, description, parameters=None) -> Callable:
    def decorator(func) -> Callable:
        if name in _registry:
            raise ValueError(f"工具名重复：{name}")     # 早失败
        _registry[name] = {"function": func, "description": description,
                           "parameters": parameters or {"type": "object", ...}}
        return func                                      # 原样返回，不改函数行为
    return decorator
```
> 两点：**重名立即抛错**（早失败）；**`return func`** 保证函数仍可直接调用。

```python
def get_tools_schema() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": ..., "parameters": ...}} ...]
```
> 遍历注册表 → 生成 OpenAI function calling 格式（发给 LLM 的能力清单）。

```python
def execute_tool(name, arguments) -> str:
    if name not in _registry:
        return f"未知工具：{name}（可用工具：{list(_registry.keys())}）"   # 不抛异常
    try:
        return str(_registry[name]["function"](**arguments))
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"                              # 不抛异常
```
> **两个设计决策**：LLM 可能编造工具名（幻觉）→ 返回提示让它自纠；工具失败不拖垮 ReAct 循环 → 错误当"观察结果"给 LLM。
> **`description` 的重要性**：LLM 完全靠它决定何时调用 → 必须写清触发场景（Prompt 工程在工具层的体现）。

### 3.2 app/tools/current_time.py（工具自声明）

```python
@register(name="current_time", description="获取当前日期和时间。当用户问'现在几点/今天几号/当前时间'时使用。",
          parameters={"type": "object", "properties": {}, "required": []})
def current_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
```
> 函数体不变（注册制只改登记方式，不改实现）。

### 3.3 app/tools/__init__.py（触发注册）

```python
from app.tools import current_time  # noqa: F401  导入即注册（副作用导入）
```
> **和 D2 models/__init__.py 同一个坑**：装饰器只在模块被导入时执行。不 import → 注册表为空 → LLM 不知道有工具。

### 3.4 agent_service.py 的两处改动

```python
# plan_node：schema 来源变了
result = await chat_with_tools([system] + messages, get_tools_schema(), trace_name="agent-plan")

# execute_node：执行方式变了（异常处理职责分离）
result = execute_tool(fn_name, args)     # 解析失败仍由引擎 try/except 兜；执行失败归注册表
```

### 3.5 app/services/memory_service.py（记忆核心）

```python
WINDOW_TURNS = 8; MAX_MESSAGES = 16; WINDOW_TTL = 86400

def _window_key(session_id): return f"session:{session_id}:window"   # 冒号分层（Redis 约定）

async def get_window(session_id) -> list[dict]:
    raw = await redis_client.get(_window_key(session_id))
    if not raw: return []
    try: return json.loads(raw)
    except json.JSONDecodeError:            # 坏数据不崩溃，退化为无记忆
        logger.warning(...); return []

async def append_turn(session_id, user_content, assistant_content):
    window = await get_window(session_id)                              # ① 读
    window.append({"role": "user", "content": user_content})           # ② 合并
    window.append({"role": "assistant", "content": assistant_content})
    window = window[-MAX_MESSAGES:]                                    # ③ 裁剪（滑窗精髓）
    await redis_client.set(_window_key(session_id),
                           json.dumps(window, ensure_ascii=False),     # 中文可读
                           ex=WINDOW_TTL)                              # 刷新 TTL
```
> **四个细节**：`window[-16:]` 切片实现滑窗；`ensure_ascii=False` 中文不转义；`ex=` 每次刷新 TTL（活跃会话不过期）；整体写回 = Upsert。

### 3.6 agent_service.py 的 run_agent（带记忆入口）

```python
history = await get_window(session_id)                                  # ① 读历史
state = {"messages": history + [{"role":"user","content": user_input}], "step_count": 0}  # ② 拼（历史在前）
result = await get_agent().ainvoke(state)                               # ③ 跑图
for msg in reversed(result["messages"]):                                # ④ 从后往前找
    if msg["role"] == "assistant" and msg.get("content"):
        final_answer = msg["content"]; break                            #    第一条有内容的
await append_turn(session_id, user_input, final_answer)                 # ⑤ 写回
```
> **④ 是个真实的坑**：工具路径下中间那些带 tool_calls 的 assistant 消息 **content 是空的** → 直接取 `messages[-1]` 会拿到空消息。

---

## 4. 类比迁移表

| 概念 | 前端类比 |
|---|---|
| 注册表 | Vite 插件系统 / 全局组件注册 |
| `@register` 装饰器 | NestJS `@Injectable()`（定义时登记到容器） |
| 副作用导入（__init__） | 模块的 side-effect import |
| 滑窗 | 环形缓冲 / 虚拟滚动"只留可视区" |
| Upsert 不 Append | 状态管理"整体替换" vs "数组 push 累积" |
| 只存对话不存工具细节 | 客户端只存业务数据，不存 RPC 调用栈 |

---

## 5. 面试考点

**Q1：工具层怎么设计的？**
→ 注册表模式：工具在自己的文件用 `@register` 声明（name/description/parameters schema），注册表集中管理。引擎只调 `get_tools_schema()` 给 LLM 看清单、`execute_tool()` 执行——**加工具不改引擎**，后期接检索/MCP 很关键。

**Q2：为什么用注册表不用 if/else 分发？**
→ if/else 是硬编码，加工具要改分发逻辑；注册表是数据驱动，新增工具零侵入。

**Q3：工具执行失败怎么办？**
→ 返回错误字符串作为"观察结果"给 LLM，让它决定重试/换工具/告知用户，**不中断 ReAct 循环**。

**Q4：Agent 怎么实现多轮记忆？**
→ Redis 滑窗：读最近 8 轮 → 拼进 messages → 处理后写回（Upsert + 裁剪 + TTL 24h）。热数据用 Redis，全量历史归 PG。

**Q5：为什么记忆不能用 append？**
→ ① 重试/重放会写入重复 ② append 单向无法裁剪最旧消息。要"读出→合并→裁剪→整体写回"。

**Q6：工具调用的中间消息为什么不进记忆？**
→ function calling 协议要求 tool_calls 与 tool 消息成对；只存一半下一轮会 400。记忆只保留对话语义。

---

## 6. 自测题（附参考答案，先自己做再看）

1. Tool Registry 解决什么问题？加新工具的流程变化？
   **参考答案**：解决"工具定义内联在引擎里、加工具要改引擎"的耦合问题。D6：改 agent_service.py 的 TOOLS dict；D8：新建工具文件 + @register 装饰器 + __init__.py import 一行，引擎零改动。

2. `@register(...)` 装饰器是怎么把函数注册进去的？什么时候执行？
   **参考答案**：展开为 `register(...)(func)`：先调外层拿到内层 decorator，Python 再把函数传给 decorator，decorator 内 `_registry[name] = {...}` 完成注册。**执行时机 = 模块被导入时**（所以 __init__.py 必须 import）。

3. 注册表三个能力各干什么？
   **参考答案**：`register` 注册工具；`get_tools_schema` 生成给 LLM 看的 schema；`execute_tool` 按名字执行（内部处理未知工具/异常）。

4. 记忆滑窗解决什么问题？为什么用 Redis？
   **参考答案**：解决 Agent 无状态导致的多轮对话丢上下文。Redis 因为对话窗口是热数据（快）+ TTL 自动过期 + 全量冷数据归 PG。

5. 为什么记忆必须 Upsert 不能 Append？
   **参考答案**：append 单向无法裁剪最旧消息；重试/重放会写入重复。必须"读出→合并→裁剪→整体写回"。

6. 为什么记忆只存 user + 最终回答，不存 tool_calls/tool 消息？
   **参考答案**：function calling 要求 tool_calls 与 tool 消息成对出现，只存一半下一轮请求会 400。记忆层只保留对话语义，工具细节留给单轮处理。

---

## 7. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| 注册表为空 | 不 import 工具模块 → @register 没执行 | `__init__.py` 副作用导入（同 D2 models 坑） |
| 取错最终回答 | 工具路径下 `messages[-1]` 的 content 为空 | 从后往前找第一条 content 非空的 assistant 消息 |
| 记忆存一半 400 | 存了 tool_calls 却没存 tool 消息 | 记忆只存 user + 最终回答（纯文本） |

---

*D8 完 ｜ 下一篇：D9 向量检索（RAG 基础：embedding + pgvector）*
