# D6 复习教程：LangGraph 状态机与 ReAct Agent

> 日期：2026-09-01 ｜ 项目：AgentForge 阶段1 ｜ 状态：已验收通过（两路径验证）

---

## 1. 今日目标

学完这一天，你应该能独立回答：

1. ReAct 是什么？一轮循环的完整流程？
2. LangGraph 的 4 个核心概念（State/Node/Edge/条件边）？
3. 为什么用 LangGraph 而不是手写 while 循环？
4. 官方 function calling 协议是什么？tool 消息为什么必须带 tool_call_id？
5. LangGraph 图是怎么"编译"和执行的？

---

## 2. 核心概念讲解

### 2.1 ReAct（Reason + Act）

**一句话**：Agent 的"思考→行动→观察→再思考"循环，直到 LLM 认为可以回答。

```
用户提问
  → LLM 思考：需要工具吗？
     是 → 声明工具调用 → 执行工具 → 观察结果 → 回 LLM 再思考
     否 → 直接回答
```

**关键**：循环次数和分支由 LLM 决定（概率性），不是写死的代码。

### 2.2 LangGraph 四概念（你确认过的类比）

| 概念 | 作用 | 你的类比 |
|---|---|---|
| **State** | 所有节点共享的状态 | pinia 全局 store |
| **Node** | 一个动作（函数） | 页面 / hooks |
| **Edge** | 固定流转路径 | 固定路由 |
| **条件边** | 按条件分发 | 动态路由 |

### 2.3 为什么 LangGraph 而非 while（面试必答）

1. **可追踪**：图结构天然可观测（D28 埋点用）
2. **可扩展**：加节点/边即可（阶段 2 多 Agent 嵌子图）
3. **可控**：recursion_limit 内置步骤上限
4. **可可视化**：能画图

### 2.4 官方 function calling（今天的重要升级）

**OpenAI 兼容协议的消息格式**（DeepSeek 也遵守）：

```
用户: 现在几点了？
助手: {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "current_time", "arguments": "{}"}}], "content": null}
工具: {"role": "tool", "tool_call_id": "call_1", "content": "2026-09-01 09:55:48"}
助手: 当前时间是 2026年9月1日 09:55:48
```

**规则**（缺了会 400）：
- assistant 消息声明 `tool_calls`（带 id）
- tool 消息必须带 **相同 id 的 `tool_call_id`** 关联
- 请求时传 `tools` schema（function 定义），LLM 才知道有哪些工具

**为什么不用"文本 JSON 伪调用"**：LLM 输出不稳定（前后带说明文字、嵌套 JSON 难解析），官方协议是结构化的、稳定的，也是所有 LLM 平台的标准——**面试直接讲"我用官方 function calling"是加分项**。

### 2.5 LangGraph 执行流程

```
get_agent() → build_graph() 组装图（节点/边/条件边）
  → graph.compile() 编译成可执行对象
  → await agent.ainvoke({"messages": [...], "step_count": 0})
  → LangGraph 从 START 开始，按边流转执行节点
  → 直到 END 或 recursion_limit（10 = 5 轮循环）
```

---

## 3. 代码逐行解读

### 3.1 工具表 + schema（TOOLS / _build_tools_schema）

```python
TOOLS = {
    "current_time": {
        "function": current_time,
        "description": "获取当前日期和时间。当用户问'现在几点/今天几号/当前时间'时使用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }
}
```
> 工具注册表雏形：函数 + 描述 + 参数 schema。D9 升级为 ToolRegistry。
> `description` 很重要——LLM 靠它决定何时调用（写得好 = 调用准）。

```python
def _build_tools_schema() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}} ...]
```
> 转成 OpenAI 的 tools 格式（`type: "function"` + function 定义）。

### 3.2 plan_node（LLM 决策）

```python
result = await chat_with_tools([system] + messages, _build_tools_schema(), trace_name="agent-plan")
assistant_msg = result["message"]
return {"messages": messages + [assistant_msg]}
```
> 走 Gateway 的 `chat_with_tools`（带 tools schema）→ LLM 返回结构化消息（可能含 tool_calls）→ 追加到 messages。
> **走 Gateway = Langfuse 自动追踪**（D5 成果直接复用）。

### 3.3 execute_node（执行工具 + observe）

```python
tool_calls = last.get("tool_calls") or []
for tc in tool_calls:
    fn_name = tc["function"]["name"]
    args = json.loads(tc["function"]["arguments"] or "{}")
    result = TOOLS[fn_name]["function"](**args)
    tool_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})
```
> 遍历 assistant 声明的每个 tool_call → 执行函数 → 构造 tool 消息（**必须带 tool_call_id**）→ 写回。
> observe（观察）内联在这里：工具结果作为 tool 消息就是"观察结果"。

### 3.4 should_continue（条件边）

```python
def should_continue(state):
    last = state["messages"][-1]
    return "execute" if last.get("tool_calls") else "answer"
```
> 判断依据是**结构化的 tool_calls 字段**（不是解析文本 JSON）——这就是官方协议的好处：判断稳定可靠。

### 3.5 组装图

```python
graph.add_edge(START, "plan")
graph.add_conditional_edges("plan", should_continue, {"execute": "execute", "answer": "answer"})
graph.add_edge("execute", "plan")    # 循环
graph.add_edge("answer", END)
return graph.compile()
```
> 4 行表达完整循环。`add_conditional_edges` 的第三个参数是"返回值 → 目标节点"映射。

---

## 4. 类比迁移表

| LangGraph 概念 | 前端类比 |
|---|---|
| StateGraph | 路由配置表 |
| State（TypedDict） | pinia / Redux store 类型 |
| Node（async 函数） | 页面 / hook / middleware |
| add_edge | 固定路由 |
| add_conditional_edges | 动态路由（按条件分发） |
| compile() | 打包构建 |
| ainvoke() | 启动应用 |
| recursion_limit | 循环上限保护 |

---

## 5. 面试考点

**Q1：ReAct 是什么？**
→ Reason + Act：LLM 循环"思考→行动→观察→再思考"，用工具扩展能力，直到能回答。模型决定每步动作。

**Q2：Function calling 和手写 JSON 解析的区别？**
→ 官方协议：LLM 原生输出结构化 tool_calls（稳定），tool 消息按 tool_call_id 关联；手写 JSON：解析不稳定（嵌套/说明文字），且不符合 API 规范会 400。生产必须用官方协议。

**Q3：LangGraph 和 LangChain 的 AgentExecutor 区别？**
→ 两者都能做 ReAct。LangGraph 更底层：显式状态机图（节点/边/条件边），可追踪、可扩展、可视化；AgentExecutor 是封装好的高层 API，灵活度低。JD 常写 LangGraph，用它是加分项。

**Q4：tool 消息为什么必须带 tool_call_id？**
→ OpenAI 兼容协议：assistant 声明工具调用（带 id），tool 返回结果必须用同一个 id 关联，LLM 才能知道"这个结果对应哪个工具调用"。缺了会 400（今天真实踩过）。

**Q5：Agent 循环怎么防止无限循环？**
→ recursion_limit（LangGraph 内置）+ step_count 计数 + 条件边保证"无工具调用就回答"。

---

## 6. 自测题（附参考答案，先自己做再看）

1. ReAct 一轮循环的完整流程？
   **参考答案**：LLM 思考（plan）→ 有工具调用则执行（execute）→ 结果写回观察（observe）→ 回 LLM 再思考；无工具调用则回答（answer）→ 结束。最多 5 轮。

2. LangGraph 的 4 个核心概念各是什么？
   **参考答案**：State=共享状态（TypedDict）、Node=动作（async 函数）、Edge=固定流转、条件边=按条件分发。

3. 为什么用 LangGraph 而非手写 while？
   **参考答案**：可追踪（图结构）、可扩展（加节点/边）、可控（recursion_limit）、可可视化。

4. 官方 function calling 中，tool 消息为什么必须带 tool_call_id？
   **参考答案**：OpenAI 兼容协议要求 tool 结果用 id 关联 assistant 声明的 tool_calls，LLM 才能对应。缺失会 400（今天踩过）。
   **（易错点：400 vs 404）**：缺 tool_call_id 是 **400 Bad Request**（请求存在但格式不合法），不是 404。400 = 内容格式错；404 = 路径/资源不存在。面试问"遇到过什么错"能区分两者是加分项。

5. should_continue 怎么判断走 execute 还是 answer？
   **参考答案**：看最后一条 assistant 消息有没有 tool_calls 字段（结构化判断，不是解析文本）。

---

## 7. 踩坑记录（今天 3 个坑）

| 坑 | 现象 | 解决 |
|---|---|---|
| 正则匹配嵌套 JSON | `\{[^{}]*\}` 匹配不了 `{"args": {}}` → 工具识别失败 | 找平衡括号逐字符扫描，不用简单正则 |
| 文本 JSON 伪调用 | tool 消息缺 tool_call_id → DeepSeek 400 | 升级官方 function calling（结构化 tool_calls + tool_call_id 关联） |
| 偶发 400 | 同格式请求有时 200 有时 400 | 根因是消息内容（tool 消息格式），不是请求格式——看错误响应体定位 |

---

*D6 完 ｜ 下一篇：D7 LLM Gateway 双通道（DeepSeek 主 / OpenAI 备）*
