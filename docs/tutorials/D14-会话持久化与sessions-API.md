# D14 · 会话持久化与 sessions API

> 补 **规划 D10「摘要记忆 + sessions API」** 的账（第 1 天，共 2 天）。
> 编号说明见本目录 `README.md`：教程 D14 的规划号是 **D10**（补账日，比教程号小），
> 不适用「教程 D09~D13 段：规划号 = 教程号 + 2」那条换算。

---

## 1. 今日目标

1. 说清「两层记忆」各自解决什么问题，以及为什么**落库要全量、喂 LLM 只留一半**
2. 让会话与消息**真的写进 PostgreSQL**（`sessions` / `messages` 两张表从 0 行变成有数据）
3. 把 `session_id` 的类型**收口为 `uuid.UUID`**，消除"一个会话两个 Redis key"的隐患
4. 补出 PRD §11 的两个查询接口：`GET /api/sessions`、`GET /api/sessions/{id}/messages`
5. 给 LLM Gateway 加上 `temperature` 参数（为 D15 的摘要用 0.1 做准备）

**验收标准**：见表末附录 A —— 数据层 14 项 + HTTP 层 12 项 + 回库核对 6 项，共 **32 项**断言全绿；
并且受本次改动影响的历史脚本回归通过。

> 📌 附录 B 记录了本轮修掉的 **3 个缺陷**（plan 抢答 / `tool_calls` 存成 JSON null /
> `recursion_limit` 注释与行为不符）—— 都已修复，并已转成可重跑的回归断言。

---

## 2. 核心概念讲解

### 2.1 缺的是哪一块

D8 做完记忆滑窗之后，对话历史**只有 Redis 一层**：

```
key: session:{id}:window    value: [最近 8 轮 user/assistant]    TTL: 24h
```

这条路的三个致命点，都是"不报错但丢数据"：

| 场景 | 结果 |
|---|---|
| 聊到第 9 轮 | 第 1 轮被挤出窗口，**永久消失**（没有任何地方留着它） |
| 隔一天再来 | TTL 到期，整个会话**从零开始** |
| Redis 重启 | 内存数据全没 |

PRD F5.3 的设计本来是**两层**：「消息全量 PostgreSQL + 热窗口 Redis」。
PG 那一层从来没建起来，所以「超 20 轮摘要」这件事也就无从谈起（没有历史可摘要）。

### 2.2 两层记忆，两条写入路径

这是今天最重要的一张认知图：

```
                    POST /api/chat
                          │
        ┌─────────────────┴─────────────────┐
        ▼                                   ▼
  Redis 热窗口（已有）                  PostgreSQL 全量（今天补）
  最近 8 轮 · TTL 24h                   messages 表 · 永久
  只存 user + assistant                user / assistant / tool 全存
        │                                   │
        └─────────────────┬─────────────────┘
                          ▼
        组装上下文：system(人设 + 摘要) + 窗口消息
```

**关键认知：这两份不是同一数据的两个拷贝，而是两条不同的路。**

| | Redis 那份 | PG 那份 |
|---|---|---|
| 用途 | **喂给 LLM** 的上下文 | **留痕 + 兜底** |
| 存什么 | 只留 `user` + `assistant` | 全量，**含 `role='tool'` 的消息** |
| 为什么 | function calling 要求 `tool_calls` 与 `tool` 消息**成对出现**，只留一半下一轮必 400 —— 所以两者的"中间态"干脆都不进窗口 | 审计、回溯、评测、以及 **Redis 失效后的回填**都要靠它；工具调用的参数与返回是排查问题时最有价值的部分 |

一句话：**"落库"和"喂 LLM"是两个决策，别用同一个标准去套。**

### 2.3 会话 id 的类型收口（本次的真正风险点）

改之前：`chat.py` 生成 `uuid.uuid4().hex` —— **32 位、无横杠**的字符串。
而 `sessions.id` 在 PG 里是 `uuid` 类型，PG 会把它规范化成 **36 位、带横杠**的标准格式。

隐藏的故障链：

```
服务端生成 "a0eebc999c0b4ef8bb6d6bb9bd380a11"
  → 拼出 Redis key: session:a0eebc999c0b4ef8bb6d6bb9bd380a11:window
  → 同一时刻落库，PG 存成 a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11
  → 前端从 GET /api/sessions 拿到的 id 是带横杠那个
  → 回传对话 → 拼出 session:a0eebc99-...:window   ← 不是同一个 key
  → 热窗口读不到 → 静默退化成"空上下文"
```

它**不报错**，只是某天你发现"这轮怎么像失忆了"。

修法不是到处 `str().replace()`，而是**把类型固定下来**：会话 id 在进程内一律是
`uuid.UUID` 对象，只在 HTTP 边界转字符串（请求侧由 pydantic 自动转）。

顺带的好处：非法值在 pydantic 层就 **422**，不用自己写正则。
旧正则 `^[A-Za-z0-9_-]{1,64}$` 会把 `"abc"` 这种值放行 —— 属于漏网。

### 2.4 一次 `/api/chat` 的完整生命周期（今天打通的就是它）

```
入口  POST /api/chat {message, session_id?}
  ↓
① 归一化 session_id：不传则 uuid4()（进程内是 UUID 对象）        ← 今天改
  ↓
② get_window(Redis)：拿最近 8 轮历史（D15 会加"未命中→从 PG 回填"）
  ↓
③ 组装：history + [本轮提问] → 喂给 LangGraph
  ↓
④ 跑 Agent（plan 决策 + 回答；需要工具时经 execute 回 plan；D6 已通）
   ⚠ 补账 Day 1 收尾时又把 `answer` 节点合并进了 `plan` —— 见附录 B.1
  ↓
⑤ 取最终回答 + 校验引用编号（D11 已通）
  ↓
⑥ append_turn(Redis)：只写 user + 最终回答                     ← 已有
  ↓
⑦ append_messages(PG)：全量写本轮产生的所有消息（含 tool 行）    ← 今天新增
  ↓
返回  {session_id, answer, sources, invalid_citations}
```

---

## 3. 代码逐行解读

### 3.1 `app/services/session_service.py`（新建，本次核心）

会话数据进库的**唯一出口**（其余模块不许直接写这两张表）。五个函数：

#### `ensure_session` —— 会话不存在则创建

```python
stmt = (
    pg_insert(Session)
    .values(id=session_id, title=_make_title(title_hint))
    .on_conflict_do_nothing(index_elements=["id"])
    .returning(Session.id)
)
```

- `pg_insert` = PostgreSQL 方言的 `INSERT`
- `on_conflict_do_nothing(index_elements=["id"])`：主键冲突时**什么都不做**（幂等）
- **为什么不用"先查再插"**：那在并发下有空窗 —— 两个请求同时查到"不存在"，于是都去插，
  后一个撞主键报错。交给数据库做原子判断，空窗就不存在了
- `.returning(Session.id)` 是个小技巧：它当**探针**用 —— 只有真的插进去了才返回一行，
  被 `ON CONFLICT` 拦下时返回 `None`。于是我们**免费**知道了"这次是不是新建的"

```python
async with async_session_factory() as db:
    created = (await db.execute(stmt)).scalar() is not None
    await db.commit()          # 不 commit 就等于没写
```

#### `append_messages` —— 一个事务里做三件事

顺序有讲究：

| 步 | 动作 | 为什么是这个位置 |
|---|---|---|
| ① | 建会话（幂等） | `messages.session_id` 是**外键**指向 `sessions.id`；会话不存在就直接插消息会触发外键报错 |
| ② | `db.add_all(rows)` 消息入队 | 此时**只在内存**，还没落库 |
| ③ | `UPDATE sessions SET updated_at = now()` | ORM 的 `onupdate` 只在**通过 ORM 更新这一行**时才触发；我们只往 `messages` 插数据、完全没碰 `sessions` 行，所以它**不会自己变**。不刷它，会话列表的"最近活跃"排序就是错的 |
| ④ | `await db.commit()` | 到这里三条语句才作为**一个整体**落库 |

```python
except Exception:
    await db.rollback()        # 任一步失败 → 整批回滚，不留半截记录
    logger.exception(...)
    raise
```

`content=(m.get("content") or "")`：`content` 列是 `NOT NULL`，而 assistant 的
"纯工具调用"消息正文是空的 —— 用空串兜底，而不是让它变成 `None` 撞约束。

#### `list_sessions` / `get_session` —— 返回 dict，不返回 ORM 对象

```python
return [{"id": str(s.id), "title": s.title, "updated_at": s.updated_at} for s in rows]
```

Session 实例带着对数据库连接的引用；`async with` 块结束时连接已归还，
**再访问它的属性会触发懒加载并报错**。在这里一次性转成纯数据，调用方拿到的就是"安全"的。

`get_session` 单独存在的理由：`GET /api/sessions/{id}/messages` 要区分
「会话不存在」（404）和「会话存在但没说过话」（200 + 空数组）。只查 `messages` 表的话，
两者都表现为空数组，前端根本分不出来。

#### `get_messages` —— 排序为什么要带 id

```python
.order_by(Message.created_at.asc(), Message.id.asc())
```

PostgreSQL 的 `now()` 返回的是**当前事务的开始时间** —— 同一个事务内**是常量**。
我们一次落两条消息（user + assistant）在**同一个事务**里，它们的 `created_at`
会**完全相同**；只按它排序，数据库可以返回任意顺序（"答在问前"）。
`id` 是自增主键、天然有序，拿它当第二排序键才稳。

### 3.2 `app/services/agent_service.py` —— 接入落库

签名变了：

```python
async def run_agent(session_id: uuid.UUID, user_input: str) -> ChatResult:
```

落库只加了几行，但"切哪一段"是关键：

```python
new_messages = result["messages"][len(history):]
await append_messages(session_id, new_messages, title_hint=user_input)
```

- `history` 是**进图之前**读出来的历史
- `result["messages"]` 是**跑完图之后**的完整消息列表
- LangGraph 只会往后追加，所以按长度切出来的**恰好是本轮新增**：
  用户提问 → 若干中间步骤（assistant 带 `tool_calls` + `tool` 返回）→ 最终回答

```python
except Exception:
    logger.exception("PG 落库失败（对话结果仍已返回）session=%s", session_id)
```

**落库失败为什么不抛**：用户已经等到回答了，此时抛 500 只会让这一轮白跑；
而且历史仍在 Redis 里，下一轮上下文不丢。
但必须留 ERROR 日志 —— **静默失败才是真正的坑**（D12 的孤儿切片就是这么攒出来的）。

### 3.3 `app/api/chat.py` —— 归一化

```python
session_id = req.session_id or uuid.uuid4()      # 原来是 uuid.uuid4().hex
...
return ChatResponse(session_id=str(session_id), ...)
```

一行之差，含义不同：进程内是 `UUID` 对象（与数据库列同类型），只在响应边界转字符串。
f-string 拼 Redis key 时 `str()` 会自动给出带横杠的标准格式，两边天然一致。

### 3.4 `app/api/sessions.py` + `app/schemas/session.py`（新建）

```python
@router.get("/sessions", response_model=list[SessionItem])
async def list_sessions_api(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    rows = await list_sessions(limit=limit, offset=offset)
    return [SessionItem(**row) for row in rows]
```

```python
session = await get_session(session_id)
if session is None:
    raise HTTPException(status_code=404, detail="会话不存在")
```

路由层只做「校验 + 调服务层 + 组装响应」，**一行 SQL 都不写**。
`session_id: uuid.UUID` 写在路径参数上，非法值会被 FastAPI 自动挡成 422。

### 3.5 `app/services/llm_gateway.py` —— temperature 变成参数

```python
DEFAULT_TEMPERATURE = 0.4
```

从 `_call_once` 一路透传到 `chat()` / `chat_with_tools()`，**默认值保持 0.4，
调用方零改动**。Langfuse 的 `model_parameters` 也同步用真实值（否则 trace 里记的是假参数）。

依据是 PRD §8.4：规划 0.2 / 回答 0.4 / **摘要 0.1** / 评测 judge 0。
摘要用 0.4 这个"创作档"，LLM 容易**加戏、改事实** —— 摘要是"忠实压缩"，必须低温度。

### 3.6 `app/prompts/summarizer.py`（新建，D15 使用）

`app/prompts/` 目录从 D1 就建了，**一直是空的**（又一处"约定建了没用起来"）。
今天把摘要 prompt 落进去，D15 直接用。文件头写明了"当前没有调用方"，避免被当成死代码。

`build_summary_user_prompt(previous_summary, new_dialogue)` 用的是**增量合并式**：
喂「旧摘要 + 本次新滑出窗口的那一段」，而不是每次全量重摘 ——
全量重摘在第 100 轮时要把 200 条消息整个喂进去，token 随轮数线性上涨。

---

## 4. 类比迁移表

| 今天的新东西 | 你已会的（TS / React / Hono / Drizzle） |
|---|---|
| PG 全量 + Redis 热窗口 | **数据库 + 内存缓存**的经典两层；Redis 是 cache，PG 是 source of truth |
| Redis 窗口只留 user/assistant | 缓存里**故意不缓存**那些"用起来会出错"的中间态（缓存的是可用形态，不是原始形态） |
| `ON CONFLICT DO NOTHING` | Drizzle 的 `.onConflictDoNothing()`；SQLite 的 `INSERT OR IGNORE` |
| `.returning()` 当"是否新建"探针 | `INSERT ... RETURNING` ≈ `upsert().returning()`，比"先查再插"少一次往返 |
| `db.add_all()` 只进内存、`commit()` 才落库 | 表单 state 改了不等于提交了；要 `submit()` 才发请求 |
| `updated_at` 必须显式 UPDATE | ORM 的 `onUpdate` 钩子只在**真的更新那一行**时才跑；只插入子表不会碰父表 |
| `now()` 在事务内是常量 | `new Date()` 一次调用取一个值；事务内的"当前时间"是**快照**，不是每行重新取 |
| 会话 id 类型收口 | TS 里 `string` vs branded `UUID` 类型 —— 让类型系统挡住"格式对但含义错"的值 |
| 返回 dict 而非 ORM 对象 | 组件卸载后还持有旧 ref 会拿到 null；连接归还后访问 ORM 属性同理 |
| 落库失败不抛但记 ERROR | `try/catch` 里"兜住但 `console.error`"，vs 静默吞掉 —— 前者可查，后者是事故温床 |

---

## 5. 面试考点

**Q1：为什么要两层记忆？只上 PostgreSQL 不行吗？**

不行（或者说没必要）：每轮对话都要读最近几轮上下文，这是**高频读**，
走 PG 每次都要查库 + 组装；Redis 一层内存读，快且 TTL 自动清理。
反过来只上 Redis 也不行 —— 超窗口 / 过期 / 重启就永久丢。
所以是「热窗口 Redis + 全量 PG」的组合：Redis 负责快，PG 负责不丢。

**Q2：为什么落库要存 `tool` 消息，而 Redis 窗口里故意不存？**

两个不同的决策：
- **喂 LLM** 时，function calling 协议要求 assistant 的 `tool_calls` 与 `tool` 消息**成对**出现，
  只留一半下一轮请求会被 400。所以窗口里连"另一半"都不留，只保留对话语义。
- **留痕** 时，工具调用的名字、参数、返回恰恰是排查问题时最有价值的信息（"这轮到底调了什么"），
  必须全量存。读取时前端可以按 `role` 自行过滤。

**Q3：`session_id` 用自增整数 / 字符串行不行？为什么用 UUID？**

UUID 有 122 位随机空间，**客户端可以自己生成**而不撞车（自增 id 做不到这点，
必须由数据库分配，多一次往返）。它也**不泄露业务信息**（自增 id 会暴露"你是第几个用户"）。
代价是索引体积大、无序 —— 本项目的会话量级完全可接受。

**Q4：`INSERT ... ON CONFLICT DO NOTHING` 和"先查再插"有什么区别？**

并发安全。先查再插在两个请求之间有空窗：都查到"不存在"→ 都去插 → 后一个撞主键报错。
`ON CONFLICT` 把"判断 + 插入"压成数据库里的一次原子操作。
`.returning()` 还能顺便告诉你"这次到底插没插"。

**Q5：落库失败了，该不该让整个请求失败？**

看这份数据是"必需"还是"补充"。这里是**补充**（回答已经产出、Redis 里已有历史），
所以选择不阻断，只记 ERROR 日志。如果落库是"必需"（比如扣款记录），就必须让请求失败。
**关键不是选择哪边，而是"选择必须是有意识的"** —— 静默吞掉永远是错的。

**Q6：为什么把 `updated_at` 的刷新写成显式 UPDATE？**

ORM 的 `onupdate=func.now()` 只在**通过 ORM 更新这一行**时触发。这里我们只往子表
`messages` 插数据，父表 `sessions` 一行都没动过 —— 框架不会"猜到"你想刷新它。
不刷，会话列表按"最近活跃"排序就是错的（排在它下面的那些会话可能比它更新）。

---

## 6. 自测题

1. `session_id` 用 32 位无横杠 hex 时，故障是怎么发生的？为什么它**不报错**？
2. `append_messages` 里三步的顺序能不能换？如果先插消息、再建会话，会发生什么？
3. 为什么 `get_messages` 的排序要带上 `id`，只按 `created_at` 会怎样？
4. Redis 窗口里不放 `tool` 消息、PG 里放，这两件事各自服务什么目的？
5. 落库抛异常时为什么选择"不阻断对话"？如果改成阻断，代价是什么？
6. 把 `answer` 节点合并进 `plan` 时，**不能**只做"删节点 + 改条件边"两步 —— 还漏了什么？漏了会怎样？
7. 想让"非工具消息的 `tool_calls` 是 NULL"，`mapped_column(JSON, nullable=True)` + 传 `None` 为什么办不到？

<details>
<summary>参考答案</summary>

1. 服务端生成无横杠 hex、PG 把它规范化成带横杠格式 → 同一会话的 Redis key 出现**两种写法**；
   前端从接口拿到带横杠的 id 回传时，拼出的 key 与最初那个不同 → 热窗口读不到，
   退化成空上下文。**不报错**是因为 Redis 查不到 key 只会返回空列表，代码把"无历史"和
   "key 不匹配"当成同一件事处理了。
2. 不能换。`messages.session_id` 是外键指向 `sessions.id`，会话不存在时插消息会触发
   **外键约束错误**，整个事务回滚。
3. `now()` 在**同一事务内是常量**，同一轮写入的 user 与 assistant 两条消息 `created_at`
   完全相同；只按它排序时顺序不确定（可能出现"答在问前"）。`id` 自增、天然有序。
4. Redis 那份是**喂 LLM 的上下文**，成对的 `tool_calls`/`tool` 缺一就 400，所以干脆都不留；
   PG 那份是**留痕与兜底**，工具调用的参数与返回是排查问题的关键线索，必须全量保留。
5. 因为回答已经产出、Redis 里历史也在，落库只是"补充"。阻断的话用户白等一轮 LLM 时间，
   体验更差。代价是可能**长期积累缺口**，所以必须配 ERROR 日志 + 监控，不能静默。
6. 漏了把 `answer` 的 system prompt（D11 的引用规范：必须标注 `[1] [2]`、只许用存在的编号）
   **搬进 `plan` 的 prompt**。漏了的话回答里不会再有编号 —— sources 照样收集了一堆，
   但没人引用，D11 的引用定位链路**静默退化**。（`check_citations` 拦不住：它只检测
   "引用了不存在的编号"，不检测"一个引用都没有"。）回归里的 `d11_citation` B1 就是守这条的。
7. 办不到。SQLAlchemy 的 `JSON`/`JSONB` 默认 `none_as_null=False` —— 传 `None` 时它会把值
   序列化成 **JSON 字面量 `null`** 存进去，而不是 SQL NULL。于是 `IS NULL` 恒为假，
   所有"靠有没有 `tool_calls` 判断消息类型"的逻辑全部失效。
   正确写法是 `JSONB(none_as_null=True)`（顺带把列类型对齐 PRD §10 的 `JSONB`）。

</details>

---

## 7. 踩坑记录

**坑 1：`updated_at` 不会自己变**
第一版以为 ORM 的 `onupdate=func.now()` 会在写入消息时自动刷新 `sessions.updated_at` ——
不会。它只在通过 ORM 更新**那一行**时触发。只插 `messages` 时 `sessions` 行完全没被碰过。
→ 显式 `UPDATE sessions SET updated_at = now()`。

**坑 2：`now()` 是事务时间，不是行时间**
同一事务写入的多行 `created_at` **完全相同**。验证脚本里"顺序为 user → assistant → tool"
这条断言，如果只用 `created_at` 排序就会**随机失败**。
→ 排序键加 `id`。

**坑 3：`Event loop is closed`（验证脚本自己的坑）**
第一版把"关闭连接池"写成独立的 `asyncio.run(_dispose())`，结果报
`RuntimeError: Event loop is closed` —— 那是**在新 loop 里去关旧 loop 的连接**。
正确做法是在**同一个 loop 内**（`run_service_layer` 末尾）`await engine.dispose()`。
这个错误不影响断言结果，但会留一段刺眼的堆栈 —— **"良性报错"也要修**，
否则将来真错误会被淹没在噪音里。

**坑 4：契约变更掀翻了下游 4 个脚本**
`session_id` 从 `str` 收口成 `uuid.UUID` 后，实测这些地方全挂：
- `d8_memory_verify.py`：`session_id = f"verify-{hex[:8]}"` → 不是合法 UUID
- `d10_agent_rag_verify.py`：`session_id=f"d10-e2e-{index}"` → 同上
- `d11_citation_verify.py`：`new_session("d11-e2e")` 返回自造前缀字符串 → 同上
- `d11_http_verify.py`：H1 断言 `len(sid) == 32`（hex 长度）→ 改动后是 36 位

**顺带挖出一个老bug**：`d8_memory_verify.py` 里写着 `r1[:60]`、`"小王" in r2` ——
它仍在按"`run_agent` 返回字符串"使用，而 D11 起返回的是 `ChatResult`。
也就是说这个脚本**从 D11 那天起就跑不通**，一跑到那行就 `TypeError`。
这正是铁律 10 里那条「改了返回类型就立刻改所有调用点」漏掉的现场。
→ 本次一并修好，并给它补上了结果汇总与失败退出码。

**坑 5：`uv` 不在非交互 shell 的 PATH 里**
`uv run ...` 直接报 `command not found: uv`。实际路径是 `~/.local/bin/uv`。
→ 脚本验证统一用绝对路径：`~/.local/bin/uv run python -m scripts.d14_sessions_verify`，
且必须**在项目根目录**执行。

**坑 6：`JSON` 列把 `None` 存成了 JSON `null`（不是 SQL NULL）**
`mapped_column(JSON, nullable=True)` 传 `None`，以为写进去的是 SQL NULL —— 不是。
SQLAlchemy 的 `JSON`/`JSONB` 默认 `none_as_null=False`，`None` 会被序列化成
**JSON 字面量 `null`**。实测 `(tool_calls IS NULL)` 对 user 行返回 `f`，
80 行里 **70 行**都是这种脏值（数据本身能存进去，所以一路都没报错）。
→ ① 模型换 `JSONB(none_as_null=True)`（同时对齐 PRD §10 的类型）；
② 迁移脚本 `scripts/d14_fix_tool_calls.py`：清洗脏值 + `ALTER TYPE json → jsonb`，带双自检。
**教训**：列类型选错不会报错，它只是"存得和你以为的不一样"。

**坑 7：`answer` 节点不是"多一层保险"，而是把同一件事做了两遍**
原以为 `plan → answer` 是"先决策、再作答"的清晰分工。实测发现：不需要工具时，
plan 的 LLM **已经把答案生成完了**；工具路径下第二次 plan 也一样。answer 只是再做一遍。
代价是白烧一次 LLM 调用 + PG 里落两条内容相近的 assistant。
→ 把 answer 的引用规范并进 plan 的 prompt，无 `tool_calls` 时直接 END。
**教训**：节点多 ≠ 职责清晰。判断依据是**实测的调用序列**（`tokens=494` + `tokens=262`），
不是图看起来的样子。

**坑 8：`recursion_limit` 从来没传给 LangGraph**
注释里写着 `recursion_limit=10 → plan 最多跑 5 次`，但 `compile()` 与 `ainvoke()` 都没传它 ——
**实际生效的是库默认值 25**。注释和真实行为差了 2.5 倍，且没有任何提示。
→ 显式设常量 `RECURSION_LIMIT = 12`（依据 v4.1 §9.2）。关键上限不能跟着库的默认值漂。

---

## 附录 A：验证与回归实测结果

### A.1 `scripts/d14_sessions_verify.py`（共 32 项断言）

**A 部分 · 数据层**（`session_service` 的出口，不调 LLM，秒级）

| 用例 | 断言要点 | 结果 |
|---|---|---|
| 前置清理 | 起始 0 条（防上一轮残留假通过） | ✅ |
| V1 `ensure_session` | 首次返回「新建」；二次返回「已存在」；标题未被二次覆盖 | ✅ |
| V2 `append_messages` | 返回条数 = 入参条数；库里**实际行数**非零且相等；顺序 `user → assistant → tool`；**tool 消息确实进库**；assistant 的 `tool_calls` 被保存 | ✅ |
| V3 `updated_at` | 用**对照会话**做相对位置断言：刚写入的排在更早活跃的之前 | ✅ |
| V4 级联删除 | 删除前 **> 0**（防空集假通过）→ 删除后 = 0 | ✅ |

**B 部分 · HTTP 层**（真实跑两轮对话，会调 LLM）

| 用例 | 断言要点 | 结果 |
|---|---|---|
| B1 | `POST /api/chat` 返回 200；`session_id` 是 **36 位**标准 UUID；回答非空 | ✅ |
| B2 | `GET /api/sessions` 能查到该会话；标题 = 首条用户消息前 30 字 | ✅ |
| B3 | `GET /api/sessions/{id}/messages` 非空；首条 `user`、末条 `assistant`；内容逐字一致 | ✅ |
| B4 | 不存在的会话 → **404**（与"空会话"区分） | ✅ |
| B5 | **无横杠写法**归一化到同一会话（查到同一批消息） | ✅ |
| B6 | 第二轮换成"必须查文档"的问题：200 且 `sources` 非空（**覆盖有工具路径**） | ✅ |

**C 部分 · 回库核对**（B 段跑完直接查表 —— 本轮修的两个缺陷只有这里看得见）

| 用例 | 断言要点 | 结果 |
|---|---|---|
| C·轮1（无工具） | 恰好 1 条无 `tool_calls` 的 assistant（修复前恒为 2 条）；非 assistant 行的 `tool_calls` 是 **SQL NULL**；回答非空 | ✅ |
| C·轮2（有工具） | 同样"恰好 1 条最终回答"（修复前 assistant 总数恒为 3）；`tool_calls` 为 SQL NULL | ✅ |

**实测输出**：`32 通过 / 0 失败`。两轮的实际角色序列：

```
轮1（无工具）：['user', 'assistant']
轮2（有工具）：['user', 'assistant', 'tool', 'tool', 'assistant']
      └ 该轮工具调用 2 次：LLM 自己决定多查一次，多轮下钻能力正常
```

### A.2 回归（全部 13 个历史脚本）

图结构与表结构都动了，所以按铁律 10 跑**全量**回归，而不是只跑受契约影响的几个：

| 脚本 | 结果 | 备注 |
|---|---|---|
| `d5_langfuse_verify` | ✅ | |
| `d6_agent_verify` | ✅ | ⭐「直答」用例消息条数 = **2**（修复前是 3）—— 抢答消失的又一实证 |
| `d7_gateway_verify` | ✅ | 主备降级正常 |
| `d8_memory_verify` | 2/2 ✅ | ⭐ 多轮记忆仍生效 → plan 兼任回答**没有**破坏历史上下文 |
| `d9_retrieval_verify` / `d9_zip_order_verify` | ✅ | |
| `d10_tool_verify` | ✅ | |
| `d10_agent_rag_verify` | 5/5 ✅ | 含"闲聊不调工具"用例 |
| `d11_citation_verify` | 7/7 ✅ | ⭐ **B1 引用可定位通过 → `[1]` 标注未退化**（prompt 合并的关键验收） |
| `d11_http_verify` | 4/4 ✅ | |
| `d12_documents_verify` | 全部通过 ✅ | |
| `d13_probe_retrieval` | ✅ | BM25 与 ts_rank 排序仍不同 |
| `d14_sessions_verify` | 32/32 ✅ | 本脚本 |

---

## 附录 B：缺陷清单与修复记录

### B.1 plan 节点"抢答"（2026-09-23 已修）

**现象**：B3 实测返回 `roles = ['user', 'assistant', 'assistant']` —— 两条 assistant。
回库看更明显：`messages` 表 80 行里，**每一轮都是成对的 assistant**。

**根因**在 D6 的图结构：`plan → (execute | answer)`。不需要工具时，`plan` 的 LLM
**已经把答案生成完了**，条件边再走 `answer` 节点又调一次 LLM 重新生成一遍。
日志印证：一轮里两次 LLM 调用（`tokens=494` + `tokens=262`）。

**影响**（三条，比"浪费"更要紧的是第 3 条）：
1. 白烧一次 LLM 调用（延迟与成本都翻倍）
2. PG 里留下两条内容相近的 assistant，`GET /api/sessions/{id}/messages` 重复展示
3. **D15 做"从 PG 回填窗口"时会回填出重复回答** —— 必须在 D15 之前修掉

**修法**：把 `answer` 节点的引用规范**整体搬进 `plan` 的 system prompt**
（`PLAN_SYSTEM_PROMPT`，逐条搬、一字未删 —— 漏了 D11 的 `[n]` 标注就会退化），
条件边改成"无 `tool_calls` → END"，`answer` 节点从图中移除。

| 场景 | 修复前 LLM 调用 | 修复后 |
|---|---|---|
| 无工具轮 | 2 次（plan + answer） | **1 次** |
| 有工具轮 | 3 次（plan 调工具 + plan 抢答 + answer） | **2 次** |

**验收证据**（脚本 C 段，回库核对）：
```
[C] 轮1·无需工具（无工具路径）roles=['user', 'assistant']
    ✅ 最终回答恰好 1 条  —— 修复前这里恒为 2 条
[C] 轮2·有工具（有工具路径）roles=['user', 'assistant', 'tool', 'tool', 'assistant']
    ✅ 最终回答恰好 1 条  —— 修复前 assistant 总数恒为 3
    └ 该轮工具调用 2 次（LLM 自己决定多查一次，多轮下钻能力正常）
```

> 有意思的是 **D10 教程第 370 行早就写下了这个猜想**："plan #2 和 answer 是两次独立的
> 生成……D11 可以评估是否合并这两步"。这次修复就是把它兑现 ——
> 从"看出成本结构不合理"到"动手合并"隔了 6 天，中间它一直以缺陷形态留在库里。

### B.2 `tool_calls` 存成了 JSON `null`（2026-09-23 已修）

**现象**：`SELECT (tool_calls IS NULL) FROM messages WHERE role='user'` 返回 `f`
—— user 消息的 `tool_calls` 竟然"不为 NULL"。

**根因**：SQLAlchemy 的 `JSON`/`JSONB` 默认 `none_as_null=False`，Python 的 `None`
会被序列化成 **JSON 字面量 `null`** 存进去，而不是 SQL NULL。实测 80 行里 **70 行**是这种脏值。

**影响**：`WHERE tool_calls IS NULL` 永远查不到行 —— 一切"靠有没有 tool_calls 判断
消息类型"的逻辑全部失效。**D15 的 PG 回填正是靠它筛消息**，所以同样是前置。
另外 PRD §10 定义的是 `JSONB`，库里实建成 `json`，类型也不符。

**修法**：① 模型改 `JSONB(none_as_null=True)`；② 迁移脚本
`scripts/d14_fix_tool_calls.py` —— 清洗脏值 + `ALTER TYPE json → jsonb`，
幂等（已是 jsonb 且无脏值就直接返回），带结构与数据双自检。

**验收证据**：
```
迁移前：列类型=json  总行数=80  JSON null 脏值=70  真工具调用=10
✅ 修复完成：json → jsonb，清洗 70 行 JSON null
✅ 数据自检通过：真工具调用 10 行原样保留，JSON null 残留 0
```

> 讽刺的是 **D09 教程第 144 行就写过**："为什么用 JSONB 不用 JSON —— JSONB 支持索引
> 和字段查询"。原则当时就懂了，只是建 `messages` 表时没贯彻到 `tool_calls` 上，
> 而且一直没暴露 —— 因为这一列在 D14 之前**从未写入过任何数据**。

### B.3 顺带发现：`recursion_limit` 注释与行为不符（已修）

代码注释写着 `recursion_limit=10 → plan 最多跑 5 次`，但 `graph.compile()` 与
`ainvoke()` **都没传过这个参数** —— 实际生效的是 LangGraph 的库默认值 **25**。

现在显式设为 **12**（依据 v4.1 §9.2：四跳下钻共 9 个节点，留 3 个余量），抽成模块常量
`RECURSION_LIMIT`。理由是**不让关键上限跟着库的默认值漂** —— 库升级改了默认值，
行为会静默变化，而注释还停在旧数字上。

### B.4 明确不做的事

- **不补 `messages.tool_results` 列**：`role='tool'` 的行本身就装着工具结果，
  再加一列是同一份数据的第二份拷贝。真要改，改 PRD §10 的文字比改库便宜。
- **不重构 `agent_service` 里已有的两个硬编码 system prompt**：那是已验收代码，
  与本日目标（补账）无关，回归风险大于收益。
  （注：B.1 把 `answer` 的 prompt 并进了 `plan` —— 那是**修缺陷的必要动作**，
  不是"顺手重构"。合并后 system prompt 反而只剩一处，更好维护。）

### B.5 下一步（教程 D15）

1. `get_window` 未命中 → 从 PG 取最近 8 轮 → 回填 Redis（PG 兜底链路）
2. 摘要压缩：轮数 > 20 触发；`summarizer` prompt（已就位）+ 游标（存 Redis）
3. 摘要注入位置：`system(人设 + 摘要) + 窗口消息`
4. 摘要用 `temperature=0.1`（参数已就位）

⚠ 回填时必须**只挑 `user` / `assistant` 进窗口**（跳过 `tool` 消息与带 `tool_calls` 的
中间消息），理由见 §2.3 —— 而 `tool_calls IS NULL` 正是筛这两类消息的依据（B.2 修的就是它）。
