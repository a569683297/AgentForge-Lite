# AgentForge-Lite 项目长期记忆

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

