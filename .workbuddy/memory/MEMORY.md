# AgentForge-Lite 项目长期记忆

## 账号体系（2026-10-21 上线，不可回退的一条分界线）

- 此后**所有新接口都必须显式声明依赖**：`CurrentUser`（登录即可）或 `AdminUser`（管理员）。
  忘了写不会报错——FastAPI 只会认为这个接口不需要鉴权，于是它就真的对匿名开放。
  上线时就是这样抓到 `GET /api/tools`（D27 时的诊断接口）漏了凭证依赖。
  自检方式：逐个看 `app/api/*.py` 的函数签名，没有 `user:` / `admin:` 参数的就是漏了。
- 数据归属靠 `app/core/tenant.py` 的 `owner_scope()`（ContextVar）+ `owner_filter()`。
  **不要在调用处写 `if owner is not None:`** —— 那正是漏过滤的来源。
  检索链路必须用 ContextVar：那一跳的调用者是 LLM，它只会填 query。
- 新增任何带 `owner_id` 的表，除了主查询，还要检查**统计量类 SQL**（BM25 的 corpus/df）。
  只给 candidates 加 owner 不报错，但排序会被别人的语料悄悄影响。
- 越权一律 **404**（不承认 id 存在）；平台级资源才用 403。没凭证是 401 + `WWW-Authenticate: Bearer`。
- `admin` 的三条自锁规则在 `app/api/admin.py`：不能禁用自己、不能降级最后一个 admin、改自己角色也走规则 2。
- bcrypt `rounds=12`（本机实测 166ms/次）。测试建账号用 `rounds=4`（0.7ms）。

## 权限体系（2026-10-23，与账号体系同一分界线）

- **三层分离，不能互相替代**：认证（你是谁 = JWT+users）/ 权限（能用哪些功能 = 权限点）/
  数据归属（能看哪几行 = owner_id）。只有 owner_id 没有权限点 → 普通用户能调管理接口；
  只有权限点没有 owner_id → 有 doc.read 的人看到全公司文档。
- **权限点目录在 `app/core/permissions.py`**，命名 `<资源>.<动作>`，15 项；admin 显式持有全集
  （不用 `*` 通配：通配会让新权限点自动给 admin 且无人 review，审计时也算不清）。
  模块级 `_self_check()` 在 import 时校验"无重复 / 无未声明 / 无孤儿"，配错即启动失败。
- **后端每个接口声明权限点**：`user: Annotated[User, Depends(require_permission(PERM_XXX))]`。
  `CurrentUser` 只留给**登录者的基本权利**（/me、改密、登出）—— 否则角色配错时用户连密码都改不了。
  `AdminUser` 保留兼容，新接口一律用 `require_permission`。
- **清单由后端下发**：`UserOut.from_user()` 填 `permissions`，出口是登录 / 刷新 / `/auth/me` / admin 用户列表。
  ⚠ 直接 `UserOut.model_validate(user)` 会让 permissions 落成空列表 → 登录 200 但菜单全没、日志无异常。
- **前端不推导 role→权限**：`lib/routes.ts` 的 `PAGE_PERMISSIONS` 是"页面要什么权限"的唯一真源，
  侧栏过滤与 `RequirePermission` 守卫共用。前端只消费后端清单。
- **`hasPermissionOf` 的三种状态**：未登录（user 为 null）→ false；
  已登录但 `permissions` 缺失（旧 session）→ true（还不知道，由启动 `/auth/me` 补齐）；
  空数组 → false（后端明确说没有）。把后两者合并会在角色配错时静默放行整个应用。
- **`tools.list` ≠ `tools.manage`**：前者是对话运行时依赖（普通用户必须有），后者是注册表管理页。
  合成一个就逼出"能对话就能改全局工具配置"或"不能对话"。
- 角色改动**立刻生效**（`get_current_user` 每请求回查库）；有用例
  `test_role_change_takes_effect_on_next_request` 钉住。别改成只信 token claims。
- 验证基线：后端 **526 passed / 3 skipped**（+40），前端 **60 passed**（+13）。

## 微信入口（2026-10-23 用户明确，**尚未实施**）

- 用户要的是 **微信 ClawBot**（官方 AI 助手插件，昵称"小龙虾"）——不是公众号、也不是企业微信自建应用。
  链路是：微信 ClawBot → **OpenClaw**（本机/服务器的智能体）→ **MCP** → AgentForge。
- ClawBot 是**私人通道**：只能本人添加、不能进群、不读聊天记录。所以它解决不了"给别人用"。
- OpenClaw 支持 MCP，配置在 `~/.openclaw/openclaw.json` 的 `mcpServers`，
  支持 `transport: "http"` + `url` + `headers.Authorization`（改完可能要 `openclaw gateway restart`）。
- AgentForge 目前只有 MCP **客户端**（`app/mcp/client.py` / `manager.py`），**没有 server 端**。
  要接这一跳，需补一个 Streamable HTTP 的 `/mcp` 端点，暴露 `search_knowledge` 一类工具。
- 已讨论但**未采纳**的方向：企业微信智能机器人长连接（我误判用户意图，已完整回滚）、
  公众号/小程序（需公网 IP + 备案 + 内容安全）。
- ⚠ 协作教训：用户问"可行吗"时**只做分析**，不要动代码；「开始做」要单独确认。

## 前端登录态的两条铁律（2026-10-21 用户实测暴露，各修过一次）

- **登录态是订阅源，不是一次性快照**：`lib/auth.ts` 的 5 个写入点统一调 `emit()`，
  `App.tsx` 用 `useSyncExternalStore(subscribeSession, getSession, getSession)`。
  ⚠ 不要写回 `useState(() => getSession())` —— 初始值函数只跑一次，
  表现为「点登录不跳转、F5 才进得去」。`getSnapshot` 必须返回稳定引用（`current` 只在写入时换）。
- **所有出站请求都必须带凭证**：axios 走拦截器，`fetch` 必须走 `authedFetch`（`api/client.ts`）。
  ⚠ 裸 `fetch(...)` 会绕过鉴权 —— 头不加、401 不刷新，
  表现为「页面全正常、唯独流式对话 401」。代码里出现裸 fetch 一律视为 bug。
  `authedFetch` 里的刷新**复用同一个 `refreshOnce()`**，不能另写一份（refresh 是轮换的）。

## 四层记忆（2026-10-21 补齐第 4 层）

| 层 | 作用域 | 载体 |
|---|---|---|
| 单轮上下文 | 一次请求 | 内存 |
| 会话热窗口（8 轮） | 一个 session | Redis `session:{id}:window` |
| 会话摘要 | 一个 session | `sessions.summary` |
| **用户画像** | **一个 user** | `user_profiles` + `user_profile_facts` |

- **判据**：换一个新会话还成立 → 才是画像。"帮我改这段代码"不是画像。
  画像**不能**塞 `sessions.summary`（那是会话级作用域，作用域错配）。
- **注入顺序**（`build_system_prompt`）：人设 → 画像 → 摘要。先稳定后临时。
  反过来会让临时上下文盖掉长期设定，不报错但画像时灵时不灵。
- **显式字段全量 / 隐式事实 top-4**：隐式会越积越长（token 线性增长）且无关事实干扰当前问题。
  一条都不匹配时兜底**按 confidence 取满 top-4**，不是 0 条（画像会在泛泛提问时消失），
  **也不是 1 条**（2026-10-21 故障：2 条事实只带 1 条 → 名字被丢 → 答"没有你的名字信息"）。
  零相关只说明「匹配没给信号」，不等于「每条都同样无关」；没信号时按 confidence 填满预算。
- **读画像 fail-open，认证 fail-closed**。判据：这层是不是业务前置条件。
  `get_profile` 捕 `Exception` 返回空 Profile —— 表不存在也不该让对话 500。
- **抽取异步、摘要同步**。判据：结果会不会被下一步立刻读到。
  `asyncio.create_task` 必须**保存强引用**（`profile_extractor._BACKGROUND`），否则可能被 GC 回收，
  症状是"有时抽有时不抽"、日志无异常。LLM 失败**必须写冷却时间**，否则一次故障变每轮重试。
- 去重三层：prompt 喂回已有事实 → 规范化（NFKC + 去空白 + 去句末标点）后 sha256 →
  `UNIQUE(owner_id, text_hash)`。前两层是效率，第三层是正确性（后台任务可能并发）。
- 匹配用布尔余弦 + **包含关系兜底**（jieba 把"前端开发"切一个词，查询"前端"匹配不上），
  但兜底**限制 token 长度 ≥ 2**（单字是任何词的子串，会退化成全部命中且无异常）。
- **两个"看着像没召回"的正常时刻**：① 本轮刚说"叫我阿梦"、同一轮问"我叫什么"→ 答不出
  （抽取排在回答之后）；② 刚在 `/profile` 改完字段、立刻在同一轮追问 → 用旧值。
  排查口径固定：**先换新会话再问**，还不行才去跑 `render_for_prompt(owner_id, query)` 探针。
- 前端 `/profile` 页不是"设置项"，是**正确性前提**：删不掉 = 纠不了错 = 功能有害。

## 部署约定（2026-10-19 踩坑记录）

- 前后端都以 Docker 容器运行：`agentforge-api` / `agentforge-web`（另加 `agentforge-db` / `agentforge-redis`）。
- **compose 里没有 bind mount**：`api` 只 `build` 不挂 `volumes`，`web` 是 nginx + 构建期 `COPY dist`。
  因此**改完源码必须重建镜像**，否则容器里跑的还是旧代码：
  ```
  docker compose build api web && docker compose up -d api web
  ```
  只改前端同样要重建 `web` —— 静态产物是构建时拷进镜像的，不是挂载的。
- 用户访问入口是 `http://localhost:8080`（web 的 nginx 把 `/api/` 反代到 `api` 容器）。
- 本机 `.venv` 只用于跑 `pytest` 与 `scripts/`，**不能替代容器内验证**：
  本机 uvicorn 跑通 ≠ 用户看到的页面已更新。
- **"是否已上线"的判定法（三处对时 + 一处探针）**，别只凭印象：
  ① `docker images --format '{{.CreatedAt}}'` 的镜像时间 **晚于**关键源文件 mtime；
  ② 容器启动时间 **晚于**镜像构建时间（`docker compose ps`）；
  ③ 容器内探针：`docker compose exec api grep <新符号>` 或
     `docker run --rm agentforge-web:local grep <新文案> /usr/share/nginx/html/assets/*.js`；
  ④ 库结构单独查（迁移不随镜像走）。
- ⚠ **`APP_ENV=dev` + 未配 SMTP 时，`/api/auth/forgot-password` 直接返回可用的 `dev_link`**
  （`_dev_link_allowed()` 的双条件）。实测能拿到 `reset-password?token=...`，
  **任何人知道邮箱就能改别人密码**。对外暴露前必须同时关掉这两个条件。
- ~~截至 2026-10-21 15:25：账号体系 + 画像的全部改动仍未 git 提交（HEAD = 96fa97b）~~
  **已过期**：2026-10-23 实测 HEAD = `3a69295 feat: 实现多租户账号体系与用户画像功能`，
  这两块已经提交。**权限体系仍是工作区改动**（未提交）。
  ⚠ 教训：这条记忆曾让我以为"全都未提交"，实际只有一部分。
  判断提交状态前先跑 `git log --oneline -2` + `git status --porcelain`，不要凭记忆。

## 已知限制

- Langfuse `GET /api/public/observations` 实测限流 **5 次/分钟**（v1 端点，2026-11-16 下线）。
  分析页一次加载需 3 页 → 已用单飞 + 负缓存 + `/api/metrics/overview` 压到 3 次。余量薄。
- `metrics.yaml` 的 `limit: 300` 只覆盖窗口内最新 300 条 observation（7 天窗口实测 3146 条），
  `token_cost` 与 `latency_p95` 是"前 300 条"口径，不是全量 —— 数字偏小，尚未修。

## 重排在容器里贴着超时线（2026-10-20 实测，未修）

- 容器内 `rerank(20 候选 @300 字)` 实测 **2.4~3.0s**，而 `settings.rerank_timeout_s` 默认 **3.0s**。
  8 次采样里 2 次降级。`D18` 记的 750~820ms 是**本机原生**成绩，容器里慢约 3 倍
  （Docker Desktop 的 Linux VM + arm64 ONNX）。
- 影响面**不止检索检查器**：真实对话超时后 Agent 拿到的是融合序而非精排序，
  日志只有一行 warning，评测与用户都看不出区别 —— 只是 Hit@k 悄悄变差。
- 检索检查器页已把这件事变成可见的（`degrade_reason` + 右列标题改"无重排"）。
  三条待选方案（调 `RERANK_TIMEOUT_S` / 缩小候选池 / 加 CPU 或换小模型）**未决，需用户拍板**。
- 排障提示：这个降级会让「重排前后名次一模一样」，容易被误读成"重排没用"，
  实际是"没跑"。判断依据只看 `effective_config` 是否等于 `hybrid_rerank`。

## 前端数据源状态（2026-10-20）

7 个页面全部接真实 API。`frontend/src/mock/` **已整目录删除**（最后一个 mock 是检索检查器的
`MOCK_RETRIEVAL`）。今后新页面**不得再引入 mock 目录** —— 没有数据源就先不做，
或把"为什么现在没有"写进页面空态，不要编数字。



- **Trace 页走 v2** `GET /api/public/v2/observations`：实时、无紧配额、支持 `?traceId=` 精确过滤。
  缺 `metadata` 与 `usageDetails`，所以该页不显示 token 与工具所属 server；
  **也不返回 `input`** —— 提问原文靠 `messages.trace_id` 反查自己的库（2026-10-20 起）。
  `/v2/observations/{id}` 与 `/v2/traces/{id}` 实测都是 404，别再试。
- **Trace 页按提问展示**：左列表每一次提问一行（带提问原文）+ 右瀑布图，
  类型筛选 dialog/inspect/eval/other（**后端过滤，先过滤再截断**）。
  根 span 名决定 kind：`run_agent`→dialog、`retriever_inspect`→inspect、`eval-judge`→eval。
- **span 状态是三态** `ok/warning/error`：`warning` 主要是重排超时降级，
  不能当 error 染红（两者处置完全不同）。`_has_error` 只认 `level=ERROR`。
- **指标仍走 v1** `GET /api/public/observations`：只有它带 `usageDetails`（token 必需），
  代价是 5 次/分钟 + 分钟级延迟。
- ⚠ **2026-11-16 两个 v1 端点同日下线**（v1 observations 与 `/api/public/traces`）。
  届时 `token_cost` / `latency_p95` 会整体降级；替代是 `/api/public/v2/metrics`，
  属**聚合接口**，口径需重新对齐，不是换 URL。
- 两套客户端缓存（`_cache` / `_v2_cache`）**必须各自独立**：键语义不同，混用会让缓存命中变成巧合。

