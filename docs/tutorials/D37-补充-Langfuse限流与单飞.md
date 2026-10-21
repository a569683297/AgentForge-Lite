# D37 补充：Langfuse 限流导致「Token 消耗 / 各阶段延迟 P95」展示失败

> 日期：2026-10-19
> 触发问题：分析页两张卡片显示「暂不可用 · Langfuse 返回 429: {message: 'Rate limit exceeded for GET /ap...'」
> 结论一句话：**不是代码写错，是每秒/每分钟配额被打爆；而配额被打爆的原因是「两个指标并发各拉 3 页」，共 6 次请求，端点只给 5 次。**

---

## 一、实测证据（不是推测）

### 1. 响应头里的硬上限

直接对 `https://cloud.langfuse.com/api/public/observations` 发请求，429 时返回：

```
x-ratelimit-limit: 5
x-ratelimit-remaining: 0
retry-after: 56
```

**该端点当前允许 5 次请求 / 分钟**（`retry-after` 说明还要等 56 秒）。这是账号计划层面的限制，代码绕不过去，只能减少请求数。

### 2. 一次页面加载要打几次

| 指标 | `metrics.yaml` 配置 | 分页计算 | 请求数 |
|---|---|---|---|
| `token_cost` | `limit: 300` | 300 ÷ `PAGE_SIZE=100` | 3 |
| `latency_p95` | `limit: 300` | 300 ÷ `PAGE_SIZE=100` | 3 |
| | | **合计** | **6** |

6 > 5。并发实测 6 个请求的状态码：

```
[200, 429, 429, 429, 429, 429]
```

只要某个指标拉的 3 页里有**任意一页** 429，`_fetch_pages` 就抛 `LangfuseUnavailableError`，`run_metric` 把它降级成 `degraded=True`，前端渲染「暂不可用」。**一个指标的 3 页必须全部成功才算成功**，所以 6 次里只要有 1 次失败，至少有一个指标必挂。

### 3. 另外四张卡为什么正常

`session_count` / `message_count` / `tool_usage` / `message_volume` 的 `source` 都是 `sql`，直接查项目 PostgreSQL，跟 Langfuse 的配额无关。

---

## 二、根因链（端到端）

```
浏览器 AnalyticsSection
  └─ Promise.all × 6 个 POST /api/metrics/query  ← 6 个请求同时飞出去
       ├─ session_count  → SQL  → PG                      ✅
       ├─ message_count  → SQL  → PG                      ✅
       ├─ tool_usage     → SQL  → PG                      ✅
       ├─ message_volume → SQL  → PG                      ✅
       ├─ token_cost     → langfuse_client.query()
       │    └─ _fetch_observations(key=(from,to,300))
       │         ├─ 缓存 miss（第一次）
       │         └─ _fetch_pages → 3 次 HTTP ──┐
       └─ latency_p95    → langfuse_client.query()        │
            └─ _fetch_observations(同一个 key)           │
                 ├─ 缓存**也是 miss**（并发，谁都还没写缓存）
                 └─ _fetch_pages → 3 次 HTTP ──┐          │
                                               ↓          ↓
                                    6 次请求 / 5 次配额 → 429
```

### 三个放大器

1. **缓存键明明相同，却没有合并**
   `_fetch_observations` 的缓存键是 `(from_ts, to_ts, limit)`，两个指标这三项完全一样 —— 串行执行时第二个必然命中缓存（0 次 HTTP）。但前端 `Promise.all` 让它们**同时**到达，两边都 miss，于是各拉各的。
   → **缺的是「单飞（single-flight）」**。

2. **失败不缓存**
   现在的设计是「只缓存成功结果」，理由是避免一次网络抖动被放大成 60 秒都读不到。这个理由对**网络抖动**成立，但 429 是**配额型失败**：`retry-after: 56` 明确告诉你 56 秒内必然继续失败。不缓存 = 每次刷新都重新把额度打光 = 越失败越打、越打越失败。
   → **缺的是「负缓存（negative cache）」**。

3. **dev 下 React StrictMode 让 effect 双跑**
   `frontend/src/main.tsx` 里包了 `<StrictMode>`，开发模式下 effect 会执行两遍，实际请求从 6 变 12。生产构建不会双跑，但 6 > 5 本身就够挂一次。
   → 这一条**不靠改 StrictMode 解决**（那是开发期正确性检查，删掉是掩盖问题），靠单飞 + 负缓存吸收。

---

## 三、三个机制（新名词就地注解）

### ① 单飞 single-flight

**是什么**：同一时刻、同一个查询，只允许一个「在途请求」（in-flight，已发出但还没返回的请求）。后来的调用者不重新发，而是**等同一个结果**。

**解决什么问题**：把「N 个并发调用 → N 次取数」压成「N 个并发调用 → 1 次取数」。

**在本次改动里怎么落**：`_fetch_observations` 维护 `self._inflight: dict[key, asyncio.Task]`。第一个调用者创建 Task 并登记；第二个调用者取到同一个 Task 直接 await。用 `asyncio.shield(task)` 包一层，保证某个调用者被取消（比如浏览器断开）不会把共享的 Task 一起取消、连累另一个调用者。

### ② 负缓存 negative cache

**是什么**：「负」指缓存的是**失败**，不是数据。

**解决什么问题**：429 之后在 `retry-after` 秒内直接短路，一次 HTTP 都不发，避免把冷却期内的配额反复打光。

**关键取舍**：冷却时长**必须从响应头读**，不能写死。
- 写死 30 秒 → 第 31 秒就重试，正好撞在还在冷却的窗口上，继续 429；
- 写死 60 秒 → 服务端第 56 秒就放行了，白白多挡 4 秒。

所以 `LangfuseUnavailableError` 上要带 `retry_after_s`，`_fetch_pages` 从 `resp.headers['retry-after']`（缺失时退回 body 的 `details.retryAfterSeconds`）解析出来。

### ③ 一次请求拿全部指标

**是什么**：后端新增 `POST /api/metrics/overview`，前端 6 个请求合成 1 个，服务端内部并发执行 6 个指标。

**为什么不是「前端串行」就够**：串行只能管住这一个页面。多标签页、Agent 工具（`app/tools/analytics.py` 走同一个 `run_metric`）、StrictMode 双跑，都会各自打一遍。**合并必须发生在离数据源最近的地方**，也就是 `_fetch_observations` 那一层。

**效果**：页面加载 6 次 → 3 次（两个 langfuse 指标合并成一次 3 页取数）；冷却期内再刷新 → 0 次。

---

## 四、自测题（待回答，答完补判定）

> 三道题都能在本文档找到出处，不要凭感觉答。

**Q1（L1，第一节 + 第二节）**
单飞合并之后，`token_cost` 和 `latency_p95` 两个请求各自拿到的是什么？是「各自的结果」，还是「同一份结果」？为什么这样不会算错（一个要 p95、一个要 sum）？

**Q2（L2，第三节 ②）**
如果偷懒写成「失败也缓存 60 秒」，会在什么情况下出错？请分别说出 `retry-after` 比 60 小、比 60 大时的后果。

**Q3（L3，第三节 ③）**
两个指标如果时间窗不同（一个近 7 天、一个近 30 天），单飞还能把它们合并成一次取数吗？为什么？

---

## 五、本次改动清单

| 文件 | 改什么 |
|---|---|
| `app/services/langfuse_client.py` | 单飞（`_inflight` + `asyncio.shield`）、负缓存（`_blocked`）、429 解析 `retry-after`、异常带 `retry_after_s` |
| `app/schemas/metrics.py` | `MetricsOverviewIn` / `MetricsOverviewItem` / `MetricsOverviewOut` |
| `app/api/metrics.py` | `POST /metrics/overview`（并校验 metric 名不重复） |
| `frontend/src/api/metrics.ts` | `queryMetricsOverview()` |
| `frontend/src/components/AnalyticsSection.tsx` | 6 个 POST → 1 个 POST |
| `tests/unit/test_langfuse_query.py` | 单飞合并用例、负缓存短路用例、`retry-after` 解析用例 |

**不在本次范围**：
- `limit: 300` 与 7 天窗口 3146 条数据的口径问题（会低估 token、P95 样本偏斜）—— 属于「数字准不准」，与「展示不出来」是两件事，单独一轮处理。
- Langfuse v1 端点 2026-11-16 下线后的 v2 迁移。

---

## 六、验证结果（2026-10-19 实测）

### ① 单飞生效（真实 HTTP）

并发查询 `token_cost` 与 `latency_p95`（同窗口 2026-10-13~19），日志：

```
10:02:33.595 Langfuse 取数起飞 窗口=... limit=300
10:02:33.595 Langfuse 取数合并到在途请求（单飞）窗口=... limit=300   ← 第二个指标搭车
10:02:40.197 Langfuse 取数完成 条数=300 页数=3 limit=300            ← 只发了 3 页
```

`_fetch_pages` 调用次数 = **1**（原来两个指标各调一次，共 6 页）。

### ② 负缓存生效

测到 429 后紧接着再查两次：

```
Langfuse 负缓存命中（限流冷却中）剩余=24s
新增 _fetch_pages 调用 = 0
```

且返回给前端的是人话：`Langfuse 接口限流（每分钟 5 次），约 24 秒后自动恢复`
（不再是截断的英文 `Rate limit exceeded for GET /ap...`）。

### ③ overview 一次拿全 6 个指标

```
session_count    degraded=False rows=  1
message_count    degraded=False rows=  1
tool_usage       degraded=False rows=  6
token_cost       degraded=False rows=  1   [{'dim': 'deepseek-chat', 'value': 416180.0, 'count': 150}]
message_volume   degraded=False rows=  3
latency_p95      degraded=False rows= 11
```

### ④ 测试

- 后端 `pytest`：288 passed / 3 skipped（新增 7 条用例覆盖单飞/负缓存/retry-after 解析）
- 前端 `vitest`：22 passed
- 前端 `tsc -b`：无错

### ⑤ 顺带修掉的一处日志 bug

`_fetch_pages` 原来用 `page` 变量记日志，300 条时打印「页数=4」—— 实际只发了 3 页
（`page` 在循环末尾自增到 4 后退出的）。排障时「日志说 4 页、实际 3 页」会让人白查一轮，
已改为独立的 `pages_fetched` 计数器。

## 七、仍然存在的问题（下一轮）

1. **余量很薄**：一次页面加载 3 次请求 / 5 次配额。正常使用够，但在同一个 60 秒窗口里
   如果缓存恰好过期又刷新两次，仍会 429（此时负缓存会挡住，用户看到的是「约 N 秒后恢复」，
   不会再雪崩）。
2. **数量口径偏小**：`limit: 300` 只取最新 300 条 observation，而 7 天窗口有 3146 条 ——
   `token_cost` 的 `count=150` 说明它只统计了 150 个带 token 的 span，
   41.6 万这个数**不是窗口总量**。要么放开 limit（撞限流），要么走 v2 metrics 聚合接口。
3. **v1 端点 2026-11-16 下线**：届时必须迁移，且 `usage.totalTokens` 在 v2 metrics
   里是否可得需要先查文档确认。

