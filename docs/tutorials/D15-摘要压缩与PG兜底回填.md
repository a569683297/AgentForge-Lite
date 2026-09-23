# D15 · 摘要压缩与 PG 兜底回填

> **编号说明**：本教程是补 PRD **D10**（摘要记忆 + sessions API）欠账的**第 2 天**，
> 对应 PRD 原文的 **F5.3 后半 + F5.1 后半 + F5.2 + §9.4**。项目教程号 ≠ PRD 规划号，
> 换算与 D03 缺号说明见 `docs/tutorials/README.md`。
>
> **本教程覆盖两段（两次提交）**：第一段 PG 兜底回填（`b211715`）+ 第二段摘要压缩与容错。
> 拆两段做、合一篇写 —— 因为它们解决的是**同一个问题的两面**：历史在 PG 里，
> 但"读得到"和"读得省"是两件事。

---

## 1. 今日目标

### 1.1 补的是哪两笔账

| PRD 条目 | 原文要求 | 补之前的状态 |
|---|---|---|
| **F5.3** | 「消息全量 PostgreSQL，热窗口 Redis」 | **只有写、没有读** —— Redis 一过期或一重启，历史永久消失 |
| **F5.1 后半** | 「超窗**先摘要**再丢弃」 | 只做了丢弃，摘要部分为零 |
| **F5.2** | 「>20 轮由 LLM 生成摘要」 | 全项目搜 `summariz` 零命中 |
| **§9.4** | 组装顺序 `system(人设+摘要) → 窗口消息` | `sessions.summary` 字段建了**无人写** |

再加一条**用户明确要求**的：
| 容错 | Redis 挂了不能让对话 500 | `redis_client.get` 裸调用 —— 所谓"兜底"只在"Redis 里没这个 key"时生效，不在"Redis 不可用"时生效 |

### 1.2 为什么顺序不能换

**先做回填、再做容错。** 没有回填，容错是**不成立的**：

```
本轮 Redis 写失败 → 窗口里没有本轮 → 下一轮未命中 → 从 PG 重建 → 自愈
                                        ↑ 这一步就是第一段做的东西
```

如果把顺序反过来（先容错后回填），"写失败 = 永久丢历史" —— 容错变成了放大器。

### 1.3 产出清单

| 文件 | 内容 |
|---|---|
| `app/services/session_service.py` | +4 个出口（`get_summary` / `save_summary` / `get_dialogue_stats` / `get_dialogue_range`）；过滤规则抽成单点 |
| `app/services/memory_service.py` | 容错三件套 + 游标读写 + `maybe_summarize` 主逻辑 |
| `app/services/agent_service.py` | `AgentState` 加 `summary`；`build_system_prompt`；`run_agent` 加 ①.5 与 ⑧.5 |
| `scripts/d15_summary_verify.py`（新） | 36 项断言，分 A/B/C/D 四段 |

---

## 2. 核心概念讲解

### 2.1 缺的是「半句话」

PRD F5.3 就一行字：**「消息全量 PostgreSQL，热窗口 Redis」**。这条要求**两个存储同时工作**。
而补账前的 `get_window()` 是这样的：

```python
raw = await redis_client.get(_window_key(session_id))
if not raw:
    return []          # ← 就是这里：Redis 没有就当"没历史"，从不去 PG 找
```

**"消息全量 PostgreSQL" 这半边只实现了写、没实现读。** 于是"全量"其实是死的 ——
历史在 PG 里躺着，但没有任何代码路径会读它来喂 LLM。

### 2.2 三层拼装：窗口 / 摘要 / 全量

补完后，"历史"由三份数据拼成，**存在两个存储里**（`window` 不是第三个存储，
它是 Redis 里那个 key 的名字）：

| 数据 | 存哪 | 管什么 | 谁写 |
|---|---|---|---|
| 热窗口 | Redis `session:{id}:window` | 最近 8 轮的**原文** | `append_turn` |
| 摘要 | PG `sessions.summary` | 更早历史的**压缩** | `maybe_summarize` |
| 全量 | PG `messages` | 一切，**权威源** | `append_messages` |
| 游标 | Redis `session:{id}:summary_upto` | 上次摘到哪条消息 | `maybe_summarize` |

读取时的**两个"优先级"方向相反** —— 这是最容易记混的一点：

| 问题 | 答案 | 由什么决定 |
|---|---|---|
| **读的时候先问谁？** | **Redis 优先** | 性能。命中即 `return`，PG 在命中路径上**根本不存在** |
| **两边冲突时信谁？** | **PG 优先** | 正确性。Redis 是副本，可能过期、残缺、重启丢光 |

**Redis 读优先，但它不是权威。** 架构上承认 Redis 可能不对，但赌它绝大多数时候是对的 ——
而"赌"的准入条件是：**写入时两边都写**（⑦ 写 Redis、⑧ 写 PG）。

### 2.3 摘要覆盖哪一段：`[游标, 窗口下界)`

这张图是第 21 轮那一刻的分布：

```
消息 id  →   1        26   27              42
             ├── 已滑出窗口 ──┤├── Redis 窗口（16 条 / 8 轮）──┤
             ↑                ↑
          游标（None）      窗口下界
             └──── 该摘的区间 ────┘
```

- **左边界的左边**（游标之前）：已经摘要过了 → 再摘一遍纯属重复
- **右边界的右边**（窗口下界之后）：还在 Redis 里活着、**每轮都完整喂给 LLM** → 再摘一遍是重复烧 token
- **中间的**：已滑出窗口、还没进过摘要 → **唯一该摘的**

所以第 21 轮该摘的是第 1–26 条，而不是全部 42 条。

### 2.4 增量合并 vs 全量重摘

| 做法 | 每次的输入 | 代价 |
|---|---|---|
| 全量重摘 | 到最新为止的**全部**历史 | token 随轮数**线性上涨**；且旧摘要被反复改写 |
| **增量合并（采用）** | 旧摘要 + 本段新滑出的 | token 基本恒定 |

**全量重摘的代价不只是 token，还有「代际漂移」** —— 第 100 轮时旧摘要已被 LLM 改写了几十次，
每一代都是对上一代的压缩，细节逐代衰减。token 是可测的成本，漂移是不可测的质量损失。

### 2.5 触发判据为什么必须从 PG 数

「超过 20 轮」这个判据，**不能从 Redis 窗口数** —— 窗口最多 16 条（8 轮），
**永远数不出 20 轮**，摘要永远不会触发。必须从 PG 数（权威源，不受 TTL 和重启影响）：

```sql
WHERE session_id = :sid AND role IN ('user','assistant') AND tool_calls IS NULL
```

`轮数 = 干净消息总数 // 2`（一轮 = user + assistant = 2 条）。

### 2.6 摘要注入位置为什么是 system

```python
system_content = PLAN_SYSTEM_PROMPT            # 人设在前
if summary:
    system_content += "\n\n【更早对话的摘要】\n" + summary   # 摘要在后
```

**为什么不往 `messages` 里插一条：**

1. 会**伪装成一条真实对话轮次** —— LLM 会以为"我说过这句话"，干扰它对轮次和引用编号的判断
2. 会产生**多条 system 消息**，而各家厂商对多 system 的兼容性参差

**为什么摘要在人设之后**：它是"更早的记忆"，越靠近当前对话越容易被用上；人设是指令，排最前更稳。

### 2.7 容错：Redis 变成"可以坏"的一层

所有 Redis 操作收敛到三个 helper，失败**只记日志、绝不向上抛**。关键在于**降级之后仍然正确**：

| 故障 | 表现 | 自愈时机 |
|---|---|---|
| **读失败** | 走 PG 兜底重建 | **当场**（本轮上下文不丢） |
| **写失败（key 不存在）** | 本轮窗口没写进去 | **下一轮**（未命中 → 重建） |
| **写失败（key 还在）** | 窗口停在上一轮 | **延迟**：下一轮会**命中旧窗口**，兜底不触发，要等 24h TTL 到期 |
| **游标读/写失败** | 退化成本轮重摘 | 结果**仍然正确**，只是多花一次 LLM 调用 |

⚠ 最后一行是"游标敢放 Redis"的**唯一理由**：**降级只损失效率，不损失正确性**。
判断一份数据该不该放缓存，标准就是那句话 —— **先问「丢了会怎样」**。

---

## 3. 代码逐行解读

### 3.1 `session_service`：4 个新出口 + 过滤规则单点化

**① 过滤规则抽成单独函数**（本次重构的关键）：

```python
def _clean_dialogue_filter(session_id):
    return (
        Message.session_id == session_id,
        Message.role.in_(("user", "assistant")),   # 排 tool 行
        Message.tool_calls.is_(None),              # 排 plan 的决策行
    )
```

三个查询（`get_recent_dialogue` / `get_dialogue_stats` / `get_dialogue_range`）共用它。
**为什么必须抽出来**：这两个条件是**成对**的一对（带 `tool_calls` 的 assistant ↔ 它触发的 tool 消息），
只要有一处漏写一条，就会出现"工具调用只留一半"→ 下一轮请求 400。
规则只写一遍，才不会三处各自跑偏。

**② `get_dialogue_stats()` —— 两个查询为什么放同一个数据库会话**

```python
async with async_session_factory() as db:
    total = ...      # 干净消息总数
    ids = ...        # 最近 window_size 条的 id（倒序）
lower = ids[-1] if (total > window_size and ids) else None
```

- `ids` 倒序，所以 `ids[-1]` 是"最近 16 条里**最早**那条" = **窗口下界**
- `total > window_size` 不成立 → 说明还没有消息滑出窗口 → 返回 `None`（没有可摘要的区间）
- **两个查询同一个 `async with`**：它们是同一时刻的一致视图。分开查的话，
  两次查询之间若有并发写入，算出的边界会互相矛盾

**③ `get_dialogue_range()` —— 返回带 `id` 是刚需**

```python
limit=max(1, limit)  +  order_by(Message.id.asc())
if after_id  is not None: conds.append(Message.id > after_id)    # 左开
if before_id is not None: conds.append(Message.id < before_id)   # 右开
```

- **左开**：游标那条本身不再摘（避免重复）
- **右开**：窗口下界那条还在 Redis 里，不归摘要管
- **为什么必须返回 id**：调用方要用"本批最后一条的 id"推进游标。少了它，
  增量摘要就退化成"每轮全量重摘"

**④ `save_summary()` 的两个刻意选择**

```python
if not summary.strip():
    return                      # ① 空摘要直接拒绝
await db.execute(update(Session).where(...).values(summary=summary))
```

- 空摘要拒绝：宁可留着上一版，也不要用空字符串把已有记忆抹掉（LLM 偶发返回空串是真的会发生）
- ⚠ **这个 UPDATE 会连带刷新 `updated_at`** —— ORM 的 `onupdate` 对任何针对该表的 UPDATE 生效，
  我们只想改 `summary`，却动了时间戳。**这条有实测证据**（脚本 A11）：
  `06:46:35.202025 → 06:46:35.260230`。影响可忽略（同一请求内、紧跟 `append_messages` 那次刷新），
  但写下来是因为它反直觉：**别以为"没写 updated_at 就没动它"**

### 3.2 `memory_service`：容错三件套

```python
async def _redis_get(key: str) -> str | None:
    try:
        return await redis_client.get(key)
    except Exception as e:
        logger.warning("Redis 读取失败，已降级 key=%s err=%s", key, e)
        return None
```

三个看似不起眼的决定：

1. **`_redis_get` 返回 `None`，于是「Redis 里没有」和「问不到 Redis」合并成一条路径** ——
   对调用方它们是同一件事：*没有可信的热数据*。少一个分支就少一处漏判。
2. **warning 而不是 error**：这不是要人半夜爬起来的问题，而是一条"已降级"的线索，对话仍然继续。
3. **不带 `exc_info=True`**：降级是**高频可预期事件**（Redis 一挂，每个请求的每次操作都走这里），
   带堆栈意味着一次故障刷出几十行 traceback，**把真正的错误埋掉**。异常类型 + 消息已经够定位。

### 3.3 游标：一个"敢放 Redis"的缓存

```python
def _summary_cursor_key(session_id): return f"session:{session_id}:summary_upto"
SUMMARY_CURSOR_TTL = 7 * 86400     # 7 天
```

- **7 天 > 窗口 TTL（24h）**：故意更长 —— 窗口过期重建后，摘要还能从上次的位置续摘
- **但仍然有 TTL**：否则每个会话永久留一个 key（内存泄漏）
- **丢了会怎样**：下次摘要从最早开始 → 退化成本轮重摘 → **结果仍正确**。
  这条不是嘴上说的，脚本 C6 实测：Redis 不可用时 `merged=34`（= 滑出的全部），而非增量的 4

### 3.4 `maybe_summarize()` —— 六步判断链

```python
① stats = await get_dialogue_stats(sid, window_size=MAX_MESSAGES)
   turns = stats["total"] // 2
   if turns <= SUMMARY_TRIGGER_TURNS:  return below_trigger      # 20 轮
② lower = stats["window_lower_id"]
   if lower is None:                   return nothing_slid_out
③ cursor = await _get_cursor(sid)
   rows = await get_dialogue_range(sid, after_id=cursor, before_id=lower, limit=40)
④ if len(rows) < SUMMARY_MIN_MESSAGES: return below_min_batch    # 4 条 = 2 轮
⑤ new_summary = await chat([...], temperature=0.1, trace_name="memory-summarize")
⑥ await save_summary(sid, new_summary); await _set_cursor(sid, rows[-1]["id"])
```

几个**必须讲清的设计选择**：

**（a）为什么返回 `dict` 而不是 `bool`**
`{"summarized", "reason", "merged"}` —— `reason` 让"没摘"也变成可断言的事实。
`bool` 的话，"没摘"只有一种表现，没法区分"轮数不够"和"游标丢了在重摘"。

**（b）频率：攒够 2 轮（4 条）再摘**
每次摘都会让 LLM **改写一遍旧摘要**，改写越频繁、细节丢失越多（代际漂移）。
攒 2 轮把调用砍一半（20 轮内 ~15 次 → ~8 次），代价是"刚滑出窗口的 1 轮"暂时不在摘要里
（下一批补上）。而摘要本身就是压缩、细节必然丢，这 1 轮的缺口影响很小。

**（c）每次只摘一批，不做 while 循环**
本函数在请求链路里**同步执行**，一次 LLM 调用 1–3 秒。循环摘会把某一次请求的延迟无限拉长。
单批 40 条（20 轮）已覆盖主路径；只有"游标丢失后的追赶"才会出现多批 —— 让它在后续几轮逐步追平。
**摘要暂时落后是可接受的降级，请求延迟失控不是。**

> ⚠ 这里与讲解阶段说的"分批循环摘"不一致，**以代码为准** —— 讲的时候没算清"循环 = 单次请求延迟不可控"这笔账。

**（d）先落摘要、再推游标 —— 顺序不能反**

```python
await save_summary(sid, new_summary)      # 先
await _set_cursor(session_id, rows[-1]["id"])   # 后
```

反过来的话：写摘要失败而游标已推进 → 游标说"摘过了"、摘要里却没有 → **这一段永久不会再被摘要**
（静默丢失记忆）。当前顺序下，最坏情况是"摘要写了、游标没推进"→ 下一轮重摘一次，只多花一次调用。
**用"多花一次调用"换"不可能丢记忆"，这笔买卖永远划算。**

**（e）同步执行、不丢后台任务**
同步 = 行为可预测、可断言、失败当场可见（学习阶段这点更重要）。代价是每约 2 轮多 1–3 秒。
将来要降延迟，换成 `asyncio.create_task` 或独立 worker 即可，**本函数的输入输出不用改**。

### 3.5 `agent_service`：摘要进 State，不进 messages

```python
class AgentState(TypedDict):
    messages: list[dict]
    step_count: int
    summary: NotRequired[str | None]        # ← 新增
```

`NotRequired` 是**兼容性设计**：历史验证脚本（如 `d6_agent_verify`）构造 state 时不传它也不会报错，
`plan_node` 用 `state.get("summary")` 取值 —— 缺了就是"没有更早的记忆"。

```python
def build_system_prompt(summary: str | None = None) -> str:
    if not summary:
        return PLAN_SYSTEM_PROMPT
    return (f"{PLAN_SYSTEM_PROMPT}\n\n"
            f"【更早对话的摘要】（本次会话更早期的内容，供参考）\n{summary}")
```

抽成**纯函数**的好处：可以单独断言"有摘要/无摘要时拼出来的东西对不对"，
不用真调一次 LLM（对比脚本 B 段就直接调它）。

`run_agent` 的两处新增：

```python
①.5  summary = await get_summary(session_id)      # 首次对话时 sessions 行还没建 → None
②    state = {..., "summary": summary}

⑧.5  try:
         await maybe_summarize(session_id)
     except Exception:
         logger.exception("摘要压缩检查失败（对话不受影响）session=%s", session_id)
```

**⑧.5 为什么排在 ⑧ 之后**：它的触发判据要从 PG 数轮数 —— 必须等本轮的 user/assistant 先落库，
否则永远差一轮。

**⑧.5 为什么敢被 try 吞掉**：它在**为下一轮**做准备 ——
跑它的时候本轮回答早已生成完，这轮用的摘要是 ①.5 读进来的。所以摘要失败只意味着
"下一轮少一份背景"，不是"本轮出错"。

---

## 4. 类比迁移表

| 本项目 | 前端对应物 | 一致的地方 |
|---|---|---|
| `get_window` Redis 命中即返回 | React Query 的 `staleTime > 0` | 缓存被信任，不重新请求 |
| `_rebuild_from_pg` 回填 | `setQueryData()` | 拿到新数据后同步写回缓存 |
| 摘要（`sessions.summary`） | 分页列表的"已归档部分" | 早期内容压缩成摘要，不再全量加载 |
| 游标 `summary_upto` | 分页游标 / `lastLoadedId` | 记录"读到哪了"，避免重复拉 |
| `_redis_get` 吞异常返回 None | axios 拦截器统一降级 | 基础设施故障不穿透到业务层 |
| 写失败不抛（只记日志） | 埋点上报失败不阻断主流程 | 辅助功能失败不影响主路径 |
| `NotRequired[summary]` | TS 的可选字段 `summary?:` | 老调用方不用改 |

---

## 5. 面试考点

1. **为什么读 Redis 优先、冲突时却以 PG 为准？**
   两个优先级方向相反，别混。读优先是**性能**决定（命中即短路，PG 不参与）；
   以 PG 为准是**正确性**决定（Redis 是副本，可能过期/残缺/丢光）。
   "允许 Redis 读优先"这个决定的准入条件是**写入时两边都写**。

2. **为什么摘要只覆盖"已滑出窗口"那段，而不是全部历史？**
   左边（游标之前）已摘过 → 重复；右边（窗口内）每轮都完整喂给 LLM → 重复烧 token。
   全量重摘还有第二个代价：旧摘要被反复改写，**代际漂移**导致细节逐代衰减。

3. **"超过 20 轮"为什么必须从 PG 数？**
   Redis 窗口最多 16 条（8 轮），**永远数不出 20 轮** —— 用它当判据，摘要永远不触发。

4. **摘要是放 system 还是放 messages？**
   system。放进 messages 会伪装成真实对话轮次，干扰 LLM 对轮次与引用编号的判断；
   而且会产生多条 system，兼容性参差。

5. **游标为什么敢放 Redis（会丢的地方）？**
   因为它**丢了只损失效率、不损失正确性** —— 退化成"重摘一遍"，结果仍然对。
   这是"一份数据该不该放缓存"的判断标准：先问丢了会怎样。

6. **Redis 写失败，历史会丢吗？**
   不会。PG 有全量。但自愈时机分两种：key 不存在 → **下一轮**；key 还在（命中旧窗口）→
   要等 **24h TTL 到期**才补齐。是**延迟自愈**，不是"立刻自愈"，也不是"永久丢失"。

7. **`save_summary` 和 `_set_cursor` 的顺序为什么不能反？**
   反了会"游标说摘过了、摘要里却没有"→ 那一段**永久**不再被摘要（静默丢记忆）。
   当前顺序的最坏情况只是"重摘一次"。

8. **为什么摘要不拆成独立模块？**
   「窗口大小（8 轮）」和「摘要边界（窗口下界）」是**同一个值**。拆两个文件就会有两处定义
   窗口尺寸，改一处忘一处就错位。而且 PRD §8.2 的架构图里 Memory Service 本来就是"滑窗 + 摘要"。

9. **降级日志为什么不用 `logger.exception`（带堆栈）？**
   降级是高频可预期事件。带堆栈 = 一次 Redis 故障刷出几十行 traceback，把真错误埋掉。
   异常类型 + 消息足够定位；真要看堆栈再把那行单独改掉。

10. **`maybe_summarize` 为什么每次只摘一批、不做循环？**
    它在请求链路里同步执行，循环摘会让单次请求延迟不可控。
    摘要暂时落后是可接受的降级 —— 让它跨几轮追平。

---

## 6. 自测题

**Q1**：第 30 轮时，窗口下界是第几条消息？该摘的是哪一段？
<details><summary>参考答案</summary>

- 30 轮 = 60 条干净消息；窗口是最后 16 条 = 第 **45–60** 条
- 窗口下界 = 第 **45** 条（最近 16 条里最早那条）
- 该摘的区间 = `(游标, 45)`。若前两轮刚摘到第 44 条，则本次该摘第 45 条之前、第 44 条之后的 —— 也就是**空**（待摘 0 条，返回 `below_min_batch`）。
  这正是"每 2 轮才攒出 4 条"的稳态：第 45、46 条要在再聊 1 轮后才一起进摘要。
</details>

**Q2**：为什么 `get_dialogue_range` 返回的每条消息都必须带 `id`？
<details><summary>参考答案</summary>

调用方要用**本批最后一条的 id** 推进游标（`_set_cursor(sid, rows[-1]["id"])`）——
它记录"下次从哪继续"。没有 id 就只能每轮从头重摘，增量摘要退化成全量重摘。
</details>

**Q3**：Redis 服务整个挂掉 5 分钟，期间有 10 轮对话。这 5 分钟里：① 对话能正常进行吗？② 历史会丢吗？③ 恢复后上下文还完整吗？
<details><summary>参考答案</summary>

① **能**。读走 PG 兜底（`_redis_get` 返回 None → `_rebuild_from_pg`），
写失败被 `_redis_set` 吞掉（只记 warning），对话不 500。脚本 C2/C4 是实测证据。
② **不丢**。PG 的 `append_messages` 不受影响，全量都在。
③ **完整**。恢复后第一次 `get_window` 未命中（窗口 key 早过期/从未写入）→ 从 PG 重建最近 8 轮。
⚠ 但注意一个细节：如果这 5 分钟里 Redis 实际可达、只是**写超时**，窗口 key 可能还残留旧内容 →
下一轮会**命中旧窗口**而跳过兜底，缺的那几轮要等 TTL 到期才补齐（延迟自愈）。
</details>

**Q4**：把"先 `save_summary` 再 `_set_cursor`"改成"先 `_set_cursor` 再 `save_summary`"，会发生什么？为什么这个顺序最坏情况更可控？
<details><summary>参考答案</summary>

反序时：游标推进成功、`save_summary` 失败（或返回空串被拒）→
下一次摘要从**新游标之后**开始，而那段内容**从没进过任何摘要** → 这段记忆**永久消失**
（游标信誓旦旦说"摘过了"）。
当前顺序的最坏情况：摘要写了、游标没推进 → 下一轮**重摘同一段**（旧摘要 + 同一批新内容，
prompt 里有"已有的事实不要重复叙述"兜着）→ 只是多花一次调用。
**一句话：用"可能多花一次调用"换"不可能丢记忆"。**
</details>

**Q5**：`build_system_prompt` 为什么抽成独立函数，而不是内联在 `plan_node` 里？
<details><summary>参考答案</summary>

为了**可测**。抽成纯函数后，可以断言"有摘要/无摘要时拼出来的 prompt 各是什么样"，
不用真调一次 LLM —— 成本从"一次付费调用"降到"一次字符串比较"。
这也是"把副作用挤到边缘、核心逻辑做成纯函数"的通用做法。
</details>

**Q6**：如果要让摘要在**第 5 轮**就生成（而不是 20 轮），最少改哪几处？
<details><summary>参考答案</summary>

只改一处：`SUMMARY_TRIGGER_TURNS = 5`。
但要意识到两个**隐含前提**：① 窗口是 16 条（8 轮），5 轮时 `window_lower_id` 是 `None`
（没有消息滑出）→ 会返回 `nothing_slid_out`，**仍然不会摘**。
所以真要让第 5 轮生效，必须**同时**把 `WINDOW_TURNS` 降到 2 或 3 ——
这正是 2.3 节说的"窗口大小与摘要边界是同一个值"的体现。
</details>

---

## 7. 踩坑记录

### 坑 1（最该记的一条：我的论证被实测推翻）

讲解阶段我给过一个论证：

> 「兜底**只读不写回** → `append_turn` 会把 Redis 写成残缺窗口 → 下一轮命中残缺窗口 → 兜底不再触发 → 历史静默丢失」

**这条不成立。** 因为 `append_turn` 第 ① 步**复用的就是 `get_window`**，它自带兜底。
第一段的 B 段对照实验（把回填临时 patch 掉）实测：**照样读回 16 条完整历史并原样写回**，
不是残缺的 2 条。

**比结论错更该说的是方法错**：我当时贴了一段带**行号和函数名**的伪代码，
注释写"重新读 Redis，读到的是空"——用精确引用的形式，去包装一个**没核实的前提**。
这比模糊表述更坏，因为它**看起来像核实过**。

回填照做（缓存 read-repair 的标准做法），但收益改写为：
① 避免同一轮查两次 PG（`get_window` 一次 + `append_turn` 内部一次）；
② 让热窗口尽早收敛回权威源。错误的论证已从 `get_window` 的 docstring 删除，换成实测结论 + 证据出处。

### 坑 2（断言写错了，不是代码错了）

第一次跑，B5 打印 `营收目标也保留=False` —— 但 B4 明明显示摘要里有「今年营收目标为1.2亿元」。
原因是**原事实写的是 `1.2 亿元`（带空格），LLM 输出成 `1.2亿元`（无空格）**，
裸子串匹配把"保住了"判成"丢了"。

**教训**：断言"关键事实不丢"时，要比**语义**不比**排版**。
现在的写法是先 `replace(" ", "")` 再匹配。这类"断言比实现更脆弱"的坑，
表现是**测试红了但代码是对的**，很容易被误判成功能有问题而乱改代码。

### 坑 3（告警噪音也是缺陷）

容错 helper 最初写的是 `logger.warning(..., exc_info=True)`。跑一次脚本，
日志里出现了 **7 段 20 行的 traceback** —— 而这还是"正常降级"的情况。
真到 Redis 挂掉时，**每个请求的每次操作**都会走这条路 → 日志被刷爆，
真正的错误被埋在噪音里。

改成 `err=%s` 带上异常消息、不带堆栈，并把这个取舍写进注释。
**"降级"和"故障"是两种事件**：降级是设计的一部分（高频、可预期），
故障需要人介入（低频、要堆栈）—— 日志级别和详略应该跟着这个区分走。

### 坑 4（签名放宽时的兼容性）

`get_window` / `append_turn` / `clear_window` 的签名从 `str` 放宽成 `uuid.UUID | str`
（历史调用方传字符串，PG 兜底需要 uuid）。风险在于：**放宽签名不会报错，只会静默改变行为**。

实测边界：`d8_memory_verify.py:56` 的"纯 Redis 用例"故意用 `trim-xxx` 这种**非 uuid** id。
处理方式是在 `_to_uuid()` 里对非 uuid 形态返回 `None` → **跳过兜底**，语义与放宽前完全一致。
回归里 `d8` 2/2 通过，就是这条兼容性的守门断言。

### 坑 5（文档里的"下一步位置"过期了）

v4.1 §13.3 写着「下一步位置仍是 D14 = PRD D16（BM25 检索实现）」，但补账插队后
**D14 被会话持久化占了**，BM25 顺延为 D16。这类"文档里写着进度"的内容最容易过期 ——
统一记进 `docs/tutorials/README.md` 的编号对照表，以那张表为准。

---

## 附录 A：验证与回归实测结果

`scripts/d15_summary_verify.py` —— **36 项断言，四段**：

| 段 | 管什么 | 项数 | 是否调 LLM |
|---|---|---|---|
| A | 触发判据 / 摘要区间 / 摘要读写 | 11 | 否 |
| B | 摘要链路（阈值 → 区间 → 压缩 → 游标 → 攒够两轮） | 13 | 是（2 次） |
| C | 容错（Redis 挂掉时读/写/摘要各自还能不能活） | 8 | 是（1 次） |
| D | 端到端：摘要真的被注入 system 了吗 | 4 | 是（1 次） |

**实测输出 `36 通过 / 0 失败`**，关键证据：

```
A3  24 条 > 窗口 16 条：total=24 且窗口下界 = 第 9 条        lower=410 ✅
A11 save_summary 连带刷新 updated_at 实测：06:46:35.202025 → 06:46:35.260230 ✅
B3  21 轮触发，摘的是「已滑出窗口」的 26 条（不是 42 条全部） merged=26 ✅
B4  摘要内容：用户提到公司项目暗号为"蓝鲸-771"，要求保密；今年营收目标为1.2亿元…（80 字，符合 ≤200 字要求）
B8  待摘 2 条（1 轮）→ 不摘（攒够 2 轮）                    ✅
B9  待摘 4 条（2 轮）→ 触发，且只摘这 4 条                   merged=4 ✅
C2  Redis 不可用时 get_window 仍返回 PG 里的 6 条历史        ✅（修复前这里 500）
C6  Redis 不可用时摘要仍摘对（游标读不到 → 退化成重摘）       merged=34 ✅
D2  模型回答：你之前告诉我，你的代号是「蓝鲸」。              ✅（窗口为空，只可能来自摘要）
```

**回归**：15 个脚本全量运行，结果见本次提交说明。

---

## 附录 B：与 PRD 的对应关系

| 本日实现 | PRD 出处 | 备注 |
|---|---|---|
| PG 兜底回填 | F5.3 + §9.4「冷数据」 | 补的是**读路径**（写路径 D14 已做） |
| 触发阈值 20 轮 | F5.2 | 「会话累计 >20 轮」 |
| 摘要存 `sessions.summary` | §10 schema | 字段 D3 就建好了，今天才有人写它 |
| 注入位置 system | §9.4「组装顺序」 | `system(人设+摘要) → 窗口消息` |
| 摘要 temp=0.1 | §8.4 场景参数 | `llm_gateway` 的 `temperature` 参数 D14 已备好 |
| 游标（`summary_upto`） | **PRD 没有** | 实现细节，我们自己定的（放 Redis，可丢） |
| Redis 容错 | **PRD 没有** | 用户本次明确要求 |

---

## 附录 C：明确不做的事（与遗留给后续的）

**不做**：

- **不补 `messages.tool_results` 列** —— `role='tool'` 的行本身就装着工具结果，
  再加一列是同一份数据的第二份拷贝（D14 的决定，继续沿用）
- **不做摘要的后台异步化** —— 同步换来可预测、可断言；异步化留到有性能诉求时
- **不把 `clear_window` 接上"清空会话"语义** —— 它只清 Redis（窗口 + 游标），
  不碰 PG 的 `sessions.summary`。真做"清空这个会话"要连 PG 一起清，等有调用方时再定

**遗留（下一步是教程 D16 = BM25 检索实现）**：

- `document_chunks` 还没有 `content_tokens` / `content_tsv` → D16 的迁移目标
- D13 留下的 `BM25_SQL`（在 `scripts/d13_probe_retrieval.py` 里）是 D16 的参考实现
- v4.1 要求 `recursion_limit=12`（D14 已改）、规划 temp=0.2（未改，属未到的要求）
