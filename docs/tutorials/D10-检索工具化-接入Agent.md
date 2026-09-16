# D10 复习教程：检索接入 Agent（RAG 工具化）

> **天序说明**：本篇内部编号 D10，实际对应 PRD-RAGPlus 的 **D11（生成 + 引用定位）前半**。
> 原因：D9 一次做完了 PRD 的 D9（切片）+ D10（向量化检索），进度比 PRD 快一天。

---

## 1. 今日目标

| 项目 | 内容 |
|---|---|
| **核心目标** | 把 D9 的 `retrieval_service.search` 包装成 Tool Registry 里的工具，让 LLM 自己决定何时检索 |
| **本质变化** | 触发权从"程序员写调用"转给"LLM 决策" |
| **技术难点** | 同步的注册表 × 异步的检索工具 |
| **验收标准** | 问只有文档里才有答案的问题 → Agent 自动调用工具 → 回答来自文档而非编造 |

**交付物**：

| 文件 | 类型 | 内容 |
|---|---|---|
| `app/tools/retrieval.py` | 新增 | `search_documents` 工具 + 结果格式化 |
| `app/tools/registry.py` | 改动 | `execute_tool` 改 async，`inspect.isawaitable` 分流 |
| `app/services/agent_service.py` | 改动 | 调用处加 `await` |
| `app/tools/__init__.py` | 改动 | 导入新工具模块（触发注册） |
| `scripts/d10_tool_verify.py` | 新增 | 工具层验证 |
| `scripts/d10_agent_rag_verify.py` | 新增 | 端到端验证 |

---

## 2. 核心概念讲解

### 2.1 从"函数"到"工具"：到底变了什么

D9 交付的是**函数**：

```python
results = await search("出差怎么报销")   # 必须有人主动写这行
```

D10 交付的是**工具**——差别有四层，不只是"谁调用"：

| 维度 | 函数（D9） | 工具（D10） |
|---|---|---|
| **触发方** | 程序员在代码里写死 | LLM 按需决策 |
| **自我描述** | 无（只有读代码的人知道） | `description` + `parameters` 进 prompt |
| **对 LLM 可见** | 不可见 | 出现在 `get_tools_schema()` 里 |
| **返回值契约** | `list[dict]`，给程序用 | `str`，给 LLM 读 |

**最容易被忽略的是第二行**：LLM 是靠 `description` 判断"这题该不该查资料"的。description 写得含糊，就会出现两种坏情况——该查不查（回答靠编），或闲聊也查（白做一次 embedding + 一次库查询，还往上下文塞无关片段）。

**所以 description 是 prompt，不是注释。**

### 2.2 注册表不做假设：`inspect.isawaitable`

D10 的技术核心。问题起点：

```python
# registry.py（D8 版本，同步）
def execute_tool(name, arguments) -> str:
    return str(_registry[name]["function"](**arguments))
```

```python
# retrieval_service.py（D9，异步）
async def search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]: ...
```

Python 里**调用协程函数不会执行它**，只返回一个 coroutine 对象。于是 `str()` 之后 LLM 收到的是：

```
<coroutine object search at 0x10a3f2c40>
```

**不报错**（只有一条容易被忽略的 `RuntimeWarning`），接口照样返回 200，只是回答质量莫名其妙地下降。这是最贵的一类 bug。

**三条路，只有一条对**：

| 方案 | 做法 | 结果 |
|---|---|---|
| **A. 注册表改 async + 运行时探测** | `inspect.isawaitable(result)` 判断要不要 await | ✅ 对 |
| B. 在工具里 `asyncio.run()` | 强行同步等异步 | ❌ `RuntimeError: asyncio.run() cannot be called from a running event loop`（execute_node 本身就在事件循环里） |
| C. 检索改回同步实现 | 用同步 session | ❌ 阻塞事件循环，`asyncio.to_thread` 白包了 |

最终实现：

```python
result = _registry[name]["function"](**arguments)
if inspect.isawaitable(result):   # 探测：是 coroutine 就 await 出来
    result = await result
return str(result)
```

**为什么"探测"比"强制全异步"好**：注册表**不该假设工具怎么实现**——查数据库、调 API、读文件，是工具自己的事。强制全异步会让 `current_time` 这种纯 CPU、零 IO 的工具白白背债（每次调用多造一个 coroutine、多一次事件循环调度）。工具作者最清楚自己要不要异步。

另外 `inspect.isawaitable` 比判 `Coroutine` 类型更宽：coroutine、Task、Future 都返回 True，能覆盖所有可等待对象。

### 2.3 结果必须格式化，且必须带编号

工具契约要求返回字符串，但**不能偷懒 `str(results)`**：

```python
# 坏：Python 字典字面量，键名每条重复，token 全浪费
[{'content': '员工出差需在返回后5个工作日内提交报销单', 'source': 'employee_handbook.md', 'similarity': 0.72}, ...]
```

```python
# 好：编号 + 空行分隔
[1] 来源：employee_handbook.md（相关度 0.72）
员工出差需在返回后 5 个工作日内提交报销单...

[2] 来源：finance_policy.md（相关度 0.65）
...
```

编号 `[1] [2]` 的价值有两层：

1. **省 token**：`'content':`、`'source':`、`'similarity':` 这些键名每条都要重复一遍，纯浪费
2. **引用锚点（更重要）**：LLM 看到编号才能在回答里写"根据 [1]"，前端才能把 `[1]` 映射回原文来源。**这是 D11 引用定位的接口契约**——没有编号，引用定位无从谈起

### 2.4 检索为空：返回明确说明，不能返回空字符串

```python
if not results:
    return f"未在知识库中找到与「{query}」相关的内容。"
```

返回 `""` 时，LLM 会理解成"工具没返回东西"，然后**用自己的知识编一个答案**——正好是 RAG 要消灭的幻觉。返回明确说明，它才知道"查了、确实没有"，才能老实回答"资料里没有"。

这跟 D8 `execute_tool` 出错返回错误文本而不是抛异常是同一思路：**让 LLM 知情，由它决策**。

---

## 3. 代码逐行解读

### 3.1 `app/tools/retrieval.py`（今日核心）

```python
from app.services.retrieval_service import search
from app.tools.registry import register
```

只导入 `search`，不导入 `add_documents` / `clear_documents`。切片与入库是**离线建库**的活，由脚本负责；工具层只需要"检索"这一个能力——权限最小化。

> 命名注意：`app/tools/retrieval.py`（工具层）与 `app/services/retrieval_service.py`（服务层）名字相近，别混。

```python
@register(
    name="search_documents",
    description=(
        "检索公司内部知识库（员工手册、财务制度、运维规范等内部文档）。"
        "当用户询问公司制度、流程、规范、政策、报销、休假、权限等内部信息时使用。"
        "不要用于寒暄、通用常识或编程问题。"
    ),
```

- `name` 要**语义化**：叫 `search_documents` 而不是 `search`——LLM 看到"search"会不确定搜什么
- `description` 三段式：**能力 → 触发条件 → 排除项**。第三段不能省，省了 LLM 会在寒暄时也去查一次

```python
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索用的查询词，用用户关注的核心概念，例如'出差报销流程'、'年假天数'",
            }
        },
        "required": ["query"],
    },
)
async def search_documents(query: str) -> str:
```

- `"type": "object"` 包 `properties` 是 JSON Schema 的固定形状（OpenAI function calling 规范）
- **`async def` 是整个 D10 的技术起点**：因为内部要 `await search(...)`，工具变成了异步的
- **参数名 `query` 必须和 `properties` 的键名逐字一致**。注册表执行是 `function(**arguments)`，`arguments` 从 LLM 返回的 JSON 解出——键名对不上就是 `TypeError: unexpected keyword argument`。**新增工具最常踩的坑**

```python
    results = await search(query)
```

直接透传，没把 `top_k` 暴露给 LLM：**不让 LLM 决定检索条数**——它没有依据判断该取 3 条还是 10 条。默认 3 条是工程决策，留在服务层。

```python
    if not results:
        return f"未在知识库中找到与「{query}」相关的内容。"
```

回显 `query` 有排查价值：能分清"确实没有"还是"检索词写偏了"。

```python
    blocks: list[str] = []
    for index, item in enumerate(results, start=1):
        source = item.get("source") or "未知来源"
        blocks.append(
            f"[{index}] 来源：{source}（相关度 {item['similarity']}）\n{item['content']}"
        )
    return "\n\n".join(blocks)
```

- `start=1`：从 1 编号。默认的 `[0]` 不符合人的引用习惯
- **`.get("source")` vs `['similarity']` 的区别不是随手写的**：
  - `get` 键不存在返回 `None` 不报错，`[]` 会抛 `KeyError`
  - `source` 入库时可选（`add_documents(source=None)`），可能真没有 → 用 `get`
  - `similarity` 是 `search()` 自己组装的，一定有 → 用 `[]`
  - 两种写法体现的是**"这个键可不可信"的判断**
- `or "未知来源"` 同时兜住 `None` 和空字符串（Python 里两者都是假值）
- `"\n\n".join(blocks)`：**分隔符在前、列表在后**，跟 JS 的 `arr.join(sep)` 顺序相反，是常见写错点

### 3.2 `app/tools/registry.py`（D10 改动）

```python
import inspect
from collections.abc import Callable
from typing import Any
```

```python
async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name not in _registry:
        return f"未知工具：{name}（可用工具：{list(_registry.keys())}）"
    try:
        result = _registry[name]["function"](**arguments)
        if inspect.isawaitable(result):
            result = await result
        return str(result)
    except Exception as e:
        return f"工具 {name} 执行失败：{e}"
```

逐行：

- 第 1 行：如果是 `async def`，这行**只造出 coroutine，一行都不执行**
- 第 2 行：`isawaitable` 探测返回值
- 第 3 行：await 出真正的返回值，覆盖 `result`
- 第 4 行：统一转字符串。对已经是字符串的返回值 `str()` 是幂等的，同步工具走这条路也安全

**容易漏的点**：这四行全在 `try` 里，**`await` 也被包住**。意思是异步工具内部抛的异常同样会被捕获、降级成错误文本。如果 `await` 写在 `try` 外面，异步工具一报错就直接冒出去打断整个 Agent 循环，**跟同步工具的行为不一致**。

函数签名从 `def` 改 `async def` 后，**所有调用点都必须加 await**——这就是第三处改动。

### 3.3 `app/services/agent_service.py`（一行）

```python
result = await execute_tool(fn_name, args)
```

`execute_node` 本身就是 `async def`，所以能 await。漏掉的话拿到 coroutine 对象，`str()` 成内存地址喂给 LLM——和 2.2 描述的坏法完全一样。

### 3.4 `app/tools/__init__.py`（一行）

```python
from app.tools import retrieval  # noqa: F401  导入即注册（副作用导入）
```

看着"没用"（导入后没引用），但有副作用：**导入模块 = 执行模块顶层代码 = 执行 `@register`**。

不写这行：`search_documents` 根本不在注册表里，`get_tools_schema()` 的清单里没有它，LLM 永远不知道有这个工具——**代码全对，功能完全不存在，且不报错**。

`# noqa: F401` 告诉 linter"这个导入没被使用是有意的"（F401 = imported but unused）。

---

## 4. 端到端运行链路（实测）

### 4.1 图结构

```
START → plan ──(有 tool_calls?)──→ execute ──→ plan（回环）
            └──(无 tool_calls)──→ answer → END
```

`should_continue` 检查最后一条消息是否带 `tool_calls`。`recursion_limit=10` → plan 最多跑 5 次。

### 4.2 一次真实问答的完整链路

问："「鸿雁」这个项目是哪个组负责的？"（答案只在文档里，模型不可能知道）

| 步骤 | 动作 | 实测 |
|---|---|---|
| 1 | **plan #1**：LLM 看到问题 + 两个工具的 schema，决定查资料 | 481 tokens / 0.96s |
| 2 | **execute**：注册表 → `search_documents(query="鸿雁 项目 负责团队")` → 格式化文本 | 检索命中 2 条 |
| 3 | **plan #2**：看到工具结果，判断信息够了，不再调工具 | 696 tokens / 1.32s |
| 4 | **answer**：基于工具结果生成最终回答 | 402 tokens / 0.75s |
| 5 | 写回 Redis 记忆 | 窗口 2/16 |

**这里有个值得注意的现象——查询改写（query rewriting）**：

用户问的是 `「鸿雁」这个项目是哪个组负责的？`，但 LLM 传给工具的 query 是 `鸿雁 项目 负责团队`——它**自己把口语句子提炼成了关键词组合**。这是 LLM 用工具的天然行为，不需要我们写任何代码，但也说明：**query 长什么样不完全受你控制**，调试检索效果时要看日志里实际传了什么。

**另一个现象**：三次 LLM 调用中，plan #2 和 answer 是两次独立的生成。plan #2 已经能判断"不需要再查了"，但它返回的那条消息并不直接作为最终回答——answer_node 又生成了一次。这是当前实现的成本结构，**D11 可以评估是否合并这两步**（把 plan #2 的回答直接复用），省一次 LLM 调用。

### 4.3 闲聊不触发工具

问"你好，你是谁" → **工具未被调用** ✅

证明 `description` 里的排除项（"不要用于寒暄"）生效了。对比一下：如果不写排除项，这次寒暄会白做一次 embedding + 一次库查询，还往上下文塞 3 段无关文本。

---

## 5. 类比迁移表

| 概念 | 前端/已学类比 |
|---|---|
| 函数 → 工具 | 内部方法 → 对外暴露的 API（带 OpenAPI 文档） |
| `description` | 接口文档里的"何时使用"说明——只不过读者是 LLM |
| `parameters` JSON Schema | OpenAPI / JSON Schema 参数定义 |
| `inspect.isawaitable` 分流 | `Promise.resolve(x)` 统一同步值和 Promise（await 一个非 Promise 也不报错） |
| `_registry` 全局字典 | 前端的插件注册表（Vite/Webpack 插件） |
| 导入即注册 | 副作用 import（`import './polyfill'`） |
| 编号 `[1] [2]` | 引用锚点，类似 markdown 的脚注引用 |

---

## 6. 面试考点

**Q1：为什么要把检索做成工具，而不是直接调？**
→ 触发权交给 LLM。用户在对话里说什么，LLM 自己判断"这题需要查资料"。直接调只会在固定的代码路径上跑，覆盖不了开放式提问。

**Q2：工具和函数的区别是什么？**
→ ① 触发方：LLM 决策 vs 程序员写死 ② 自我描述：工具带 description/parameters 进 prompt，LLM 靠它判断何时用 ③ 返回值必须是人可读的字符串（LLM 只认文本）④ 出错返回错误文本而非抛异常，让 LLM 知情决策。

**Q3：同步注册表怎么调用异步工具？**
→ 注册表改 async，运行时用 `inspect.isawaitable` 探测返回值分流：同步原样返回，异步 await。不假设工具实现方式，两种共存，旧工具零改动。

**Q4：为什么不强制所有工具都写成异步？**
→ 无 IO 的工具（如 `current_time`）改异步是白背债：每次调用多造 coroutine、多一次事件循环调度。工具作者最清楚自己要不要异步。

**Q5：漏 await 会怎么样？**
→ 不报错，只是拿到 coroutine 对象，`str()` 成 `<coroutine object ... at 0x...>` 喂给 LLM。接口 200、无异常日志、回答质量变差——生产环境最难查的一类 bug。

**Q6：为什么工具返回的结果要带 `[1] [2]` 编号？**
→ ① 省 token（不用重复键名）② 引用锚点：LLM 能写"根据 [1]"，前端能映射回来源。这是 RAG 可溯源性的基础。

**Q7：检索没结果时为什么要返回一句说明？**
→ 返回空字符串会让 LLM 以为"工具没给东西"而自己编答案。返回明确说明，它才知道该老实回答"资料里没有"。

---

## 7. 自测题（先自己做，再看参考答案）

1. D9 的 `search()` 和 D10 的检索工具，本质区别是什么？至少说三点。
2. 为什么 `execute_tool` 必须改成异步？不改的话是报错还是别的？
3. `inspect.isawaitable` 这个判断，比"要求所有工具都写成异步"好在哪？
4. 工具为什么返回带编号的文本，而不是 `str(list[dict])`？说出两个理由。
5. 新增一个工具文件后，为什么必须在 `app/tools/__init__.py` 里 import 一次？不写会怎样？
6. 工具函数的参数名和 `parameters.properties` 里的键名不一致会发生什么？
7. 检索为空时返回空字符串会引发什么问题？

<details>
<summary>参考答案</summary>

1. ① 触发方：函数要程序员主动调用，工具由 LLM 决策 ② 可见性：函数对 LLM 不可见，工具通过 `get_tools_schema()` 进 prompt ③ 自我描述：工具带 description/parameters ④ 契约：工具返回字符串给 LLM 读，函数返回结构化数据给程序用。
2. 不改 → 异步工具被同步调用，只返回 coroutine 对象，`str()` 成内存地址喂给 LLM。**不报错**，只有一条容易被忽略的 `RuntimeWarning`，属于静默错误。
3. 注册表不该假设工具怎么实现。同步工具原样返回（不白造 coroutine、不多一次调度），异步工具自动 await，旧工具零改动、新工具不受限。而且 `isawaitable` 对 coroutine/Task/Future 都返回 True，覆盖面比判具体类型宽。
4. ① 省 token：`str(dict)` 会让 `'content':`、`'source':`、`'similarity':` 这些键名每条重复一遍，还有 Python 字面量语法噪音 ② 引用锚点：编号是 D11 引用定位的接口契约，LLM 才能写"根据 [1]"，前端才能映射回原文来源。
5. 导入模块才会执行模块顶层的 `@register` 装饰器。不写 → 工具没进注册表 → `get_tools_schema()` 里没有它 → LLM 永远不知道有这个工具。**代码全对但功能不存在，且不报错**。
6. `function(**arguments)` 抛 `TypeError: unexpected keyword argument`。因为 `arguments` 是从 LLM 返回的 JSON 解出的 dict，键名对不上就传不进去。这是新增工具最常踩的坑。
7. LLM 会理解成"工具没返回东西"，转用自己的知识编一个答案——正是 RAG 要消灭的幻觉。应返回"未在知识库中找到与「X」相关的内容"，让它知道该老实回答"资料里没有"。

</details>

---

## 8. 踩坑记录

| 坑 | 现象 | 解决 |
|---|---|---|
| 同步注册表调异步工具 | 不报错，LLM 收到 `<coroutine object ...>` | `execute_tool` 改 async + `inspect.isawaitable` 分流 |
| 漏改调用点 | 改了注册表但没给 `execute_tool` 加 await，问题依旧 | 签名改成 async 后，**所有调用点都要检查** |
| 异步工具的异常没被兜住 | 若 `await` 写在 `try` 外，异步工具报错会打断整个 Agent 循环 | `await` 必须写在 `try` 内部 |
| 新增工具不生效 | 代码全对但 LLM 不知道有这工具 | `app/tools/__init__.py` 里必须 import 一次（导入即注册） |
| 参数名对不上 | `TypeError: unexpected keyword argument` | 函数参数名与 `parameters.properties` 键名逐字一致 |
| 验证脚本被环境拦截 | 沙箱拦 `.env` 读取（PermissionError）→ 脚本跑不起来 | 由用户在自己的终端执行；纯逻辑部分可用 importlib 绕开配置层单独验证 |

---

*D10 完 ｜ 下一篇：D11 引用定位与来源展示（让回答中的 [1] 能跳回原文）*
