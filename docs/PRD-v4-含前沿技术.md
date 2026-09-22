# AgentForge 阶段 1 — 详细产品需求文档（PRD v4.0 · 含前沿技术融入版）

> **文档版本**：v4.0（在 v3.0 基础上融入 Harness.io / Dify / Agentic BI 三项前沿技术）
> **编制日期**：2026-09-22
> **原文档**：`docs/PRD.md`（v3.0，2026-08-28）—— **本文件不覆盖原文档，两者并存**
> **配套分析**：`docs/概念融合评估-2026-09-21.md`（v4，含可行性判定与可执行接入步骤）
> **当前进度**：账面 D13 / D34（教程 D01–D13；D10 摘要记忆为已知欠账）
> **演进关系**：阶段 1（RAG+ 增强版，**37 天核心 + 5 天缓冲，上限 42 天**）→ 阶段 2（多 Agent 版，+14 天）→ 阶段 3（完整 Runtime 平台，+14 天）

---

## 0. 本版相对 v3.0 的变更总览

### 0.1 三项技术的融入方式

| 技术 | 融入方式 | 新增天数 | 含金量类型 | 接入位置 |
|---|---|---|---|---|
| **Harness.io MCP** | **并入 D26/D27**（MCP 那两天本来就在做） | **0 天** | 协议层：真实外部企业系统互操作 | `mcp_client.py` → Harness 官方 MCP Server |
| **Agentic BI** | **新增 D35–D36** | 2 天 | 能力层：自主多步分析 + 发挥前端主力 | Tool Registry + `metrics.yaml` 语义层 + ECharts |
| **Dify 对照评测** | **新增 D37** | 1 天 | 方法论：把"为什么自建"从论证表变实测数据 | Eval Service（judge 增加外部答案输入源） |

**合计新增 3 天**：核心 D1–D34 → **D1–D37**；缓冲 D38–D42（5 天）；总上限 **42 天**。

### 0.2 里程碑编排的三条铁律（本版最重要的设计决策）

1. **D1–D34 的编号与内容保持不变**——已完成 13 天的进度记录、教程（D01–D13）、复习排期（1-3-7-14）与进度核对报告**全部不受影响**，无需任何回溯修正。
2. **能并入原有天的绝不新增天数**——Harness 接入并入 D26/D27（MCP 客户端本就必写），前端图表并入 D30/D31（ChatPage 本就有图表能力）。
3. **只有真正的新能力才编新天数**——Agentic BI 语义层（D35–D36）与 Dify 对照评测（D37）是 v3.0 范围外的新增能力，因此单独编号。

> ⚠️ **口径说明**：原 v3.0 表格中 D35–D39 为缓冲天。本版将 D35–D37 改为新增工作天，缓冲顺延为 D38–D42。这不是"加需求"，而是把两项新能力显式排期，避免它们以"顺手做做"的名义挤占缓冲、最终挤掉验收。

### 0.3 未变更的部分（明确列出，避免误解）

以下章节**完全沿用 v3.0**，本版仅做必要的一致性同步：
- 技术栈与选型（§8.4）
- 分层架构红线（§8.2）
- F1–F10 功能需求（§7，仅 F6 有扩展）
- RAG 检索链路与 BM25/tsvector 的术语澄清（§9.3）
- 数据模型主体（§10，仅新增 1 张表）
- 测试策略（§14）
- 既有 S1–S9 成功指标（§4，仅新增 S10–S14）

---

## 1. 文档信息

| 项 | 内容 |
|---|---|
| 项目名称 | AgentForge（阶段 1：RAG+ 增强版 + 前沿技术融入） |
| 项目代码 | agentforge-stage1 |
| 项目根目录 | `/Users/779369901qq.com/workspace/bs/AgentForge-Lite/` |
| 主要语言 | Python 3.12（后端）+ TypeScript（前端） |
| 开发方式 | AI 辅助 + 人工审查，每模块先讲原理再写码 |
| 目标岗位 | 西安/远程 AI-Agent 开发岗（12-50k） |
| 阶段 1 周期 | D1–D37 核心（37 天）+ D38–D42 缓冲（5 天），上限 42 天 |

---

## 2. 项目背景

### 2.1 需求来源
基于深圳 4 份 + 西安 8 份 + 国内/海外远程 6 份共 **18 份真实 AI-Agent 开发岗位 JD** 的能力图谱调研：

**100% 高频考点**：LangChain/LangGraph、RAG 全流程、Tool Use/Function Calling、Python 后端（FastAPI/Flask）
**高薪岗差异化考点（20-50k）**：Agent 效果评估体系（阿里云点名）、多智能体协作（阿里云/华为）、记忆管理（华为）、MCP 协议（远程岗必考）、可观测性（海外远程岗硬要求）、AI 编程工具重度使用（阿里云）

### 2.2 机会窗口
- 市场上大量求职者项目停留在"RAG 问答 demo"层（教程复刻，无评测、无观测、无工具）
- 阿里云/华为 JD 明确要"效果评估体系建设"，**会用数据驱动优化 Agent 的人极少**
- **2026 年新增窗口**：主流平台纷纷"把自己变成别人的工具"（Harness 开源 MCP Server、Dify 内置 MCP 服务端）。**"能与企业系统互操作的 Agent" 成为新的能力分界线**，而大多数求职者项目仍是孤岛。

### 2.3 为什么自建而非用 Dify/RAGFlow

| 维度 | Dify 等平台 | 自建（本项目） |
|---|---|---|
| 检索链路可控性 | 黑盒，不可调 | 全链路自研，可做消融实验 |
| 效果可评测 | 无内置评测体系 | 评测集 + LLM-as-judge + 消融 |
| 可演进性 | 平台限制 | 分层架构，可升多 Agent/平台 |
| 面试说服力 | "我用了 Dify" | "我实现了 xxx，数据证明 xxx" |

> **v4.0 重要升级**：本版不再只用"论证表"回答这个问题，而是**用实测数据回答**（D37 对照组评测：同一批问题分别由 Dify 应用与 AgentForge 作答，用同一 judge 打分）。"为什么自建"从观点变成证据。

### 2.4 v4.0 新增的差异化来源（为什么加这三项）

| # | 新差异化点 | 对应 JD 要求 | 为什么大多数人做不到 |
|---|---|---|---|
| 1 | **外部系统 MCP 互操作** | MCP 协议（远程岗必考）；企业级工程化（高端岗第 2 项） | 多数人只写过自研 MCP server，没接过真实企业平台 |
| 2 | **Agentic BI 自主多步分析** | 数据分析 Agent（高端岗第 8 项） | 多数人只做文档问答，不敢碰结构化数据（怕 SQL 出错） |
| 3 | **语义层设计** | 架构设计能力 | "让 LLM 不写 SQL"是反直觉决策，讲出来就是设计品味 |
| 4 | **平台对照评测方法论** | 效果评估体系（阿里云点名） | 多数人只评自己，没做过"自建 vs 平台"的横向对照 |

---

## 3. 产品定位与价值

**一句话定位（v4.0 升级）**：企业知识库驱动的智能问答 **与数据分析** Agent 平台——让企业上传私有文档后，获得一个能带引用回答、能调外部工具（**含企业级平台**）、能**自主多步分析结构化数据**、能记住上下文、**且效果可量化、过程可观测、决策可证伪**的 AI 助手。

**四句价值主张**：
1. **答得准**：混合检索 + 重排，答案带可点击的原文引用
2. **有依据**：评测体系用数据证明"我的系统效果比基线好 10%+"
3. **能落地**：Docker 一键部署，全链路可观测，可对接真实业务工具（MCP）
4. **能互操作**（v4.0 新增）：能接入企业级平台（Harness）与主流 LLM 平台（Dify），而非自建孤岛

---

## 4. 目标与成功指标（验收底线）

### 4.1 原有指标（S1–S9，不变）

| # | 指标 | 目标值 | 验证方式 | 优先级 |
|---|---|---|---|---|
| S1 | 评测集规模 | ≥ 50 条（3 类） | 评测集入库文件 | 必须 |
| S2 | 消融实验提升 | 混合+重排 vs 纯向量 ≥ +10% | 评测报告 | 必须 |
| S3 | 最终准确率 | ≥ 88% | LLM-as-judge 自动评分 | 必须 |
| S4 | 端到端延迟 | P95 < 5s | 压测脚本 | 必须 |
| S5 | 引用覆盖率 | ≥ 90% 答案含引用且可定位 | 评测报告 | 必须 |
| S6 | Langfuse 追踪 | 100% 对话可追踪 | 面板抽查 | 必须 |
| S7 | MCP 工具 | ≥ 1 个真实外部工具可用 | 演示脚本 | 必须 |
| S8 | 测试覆盖率 | 核心链路 ≥ 60% | pytest --cov | 应该 |
| S9 | 部署 | docker compose up 一条命令 | 全新机器 | 必须 |

### 4.2 v4.0 新增指标（S10–S14）

| # | 指标 | 目标值 | 验证方式 | 优先级 |
|---|---|---|---|---|
| S10 | **外部 MCP 互操作** | 接入 ≥ 1 个**非自研** MCP Server（Harness），工具可被发现并成功调用 | 演示脚本 + Langfuse 追踪 | 必须 |
| S11 | **语义层覆盖** | `metrics.yaml` 含 ≥ 3 个指标定义，含 ≥ 1 个支持多步分析的场景 | 配置文件 + 演示 | 必须 |
| S12 | **多步分析正确率** | 评测集类型 D（多步分析）≥ 70% 通过 | 评测报告 | 应该 |
| S13 | **工具写操作防护** | 100% 写操作需显式确认；SQL 注入测试用例全拦截 | 安全测试脚本 | 必须 |
| S14 | **平台对照报告** | 产出 Dify vs AgentForge 同题对照报告（≥ 20 条） | Markdown 报告 | 应该 |

> **S7 的口径澄清（重要）**：v3.0 中 S7 的"真实外部工具"由自研的 `inventory_server`（内存 mock 数据）承担，**严格讲不满足"真实外部"**。v4.0 后 S7 由 **S10 的 Harness 外部 Server** 真实满足，而 `inventory_server` 转为"自研 server（理解协议两端）"的教学与测试用途。**两者都需要保留**——自己写 server 证明懂协议，接别人的 server 证明能互操作。

---

## 5. 用户角色与核心场景

### 5.1 角色
| 角色 | 说明 | 使用方式 |
|---|---|---|
| 终端用户 | 向 Agent 提问，获得带引用答案或数据分析结论 | Web 对话页 / 分析页 |
| 知识管理员 | 上传/删除文档，查看入库状态 | Web 知识库页 |
| **运维/研发用户**（v4.0 新增） | 让 Agent 诊断外部平台（Harness）流水线失败 | Web 对话页（工具调用） |
| **数据分析用户**（v4.0 新增） | 问业务指标趋势，获得图表 + 结论 | Web 分析页（Agentic BI） |
| 系统管理员（阶段 3 完善） | 查看评测报告、追踪面板、MCP Server 管理 | Web 管理页 |

### 5.2 核心场景（端到端叙事）

**场景 A：文档问答（RAG）** — 不变
> 管理员上传《公司考勤制度.pdf》→ 系统解析、切片、向量化入库（status: processing → ready）→ 用户问"年假怎么算"→ Agent 引擎判断需要检索 → RAG 混合检索 top5 → 重排 → 组装 prompt → 模型生成答案并标注 [1][2] → UI 展示答案 + 可点击引用 → 全程进入 Langfuse 追踪

**场景 B：工具调用（Tool Use，自研 MCP Server）** — 范围澄清
> 用户问"库存表里数量小于 10 的商品"→ Agent 规划：需要查数据库 → 调用 `query_inventory` 工具（经 Tool Registry / MCP Client → 自研 `inventory_server`）→ 返回结果 → Agent 总结为自然语言 → 工具调用过程在追踪面板可见
> **定位**：证明"我能写出一个合规的 MCP Server"，也是 MCP 客户端的第一条验证路径（可控、可测）。

**场景 C：多轮记忆（Memory）** — 不变
> 用户先问"上季度营收"→ 再问"环比呢"→ Agent 从滑窗记忆中取出上下文 → 正确理解"环比"→ 回答 → 超过 20 轮后自动摘要压缩历史，摘要作为长期记忆

**场景 D：评测与优化（差异化核心）** — 不变
> 管理员点击"运行评测"→ 系统用 50 条评测集跑 3 个检索配置 → LLM-as-judge 自动打分 → 生成 Markdown 报告 → 管理员查看报告 → 选择最优配置作为线上默认

**场景 E（v4.0 新增）：外部系统互操作（MCP 接入企业平台）**
> 用户问"我最近有哪些流水线失败了？为什么？"
> → Agent 识别需要外部工具 → 从 Tool Registry 选中 `harness_diagnose`（该工具**不是写死在代码里的**，而是启动时从 Harness 官方 MCP Server 动态发现的）
> → MCP Client 走 stdio 把 JSON-RPC `tools/call` 发给常驻的 `harness-mcp-v2` 子进程
> → 子进程带 PAT 调用 Harness 云端 API，返回失败流水线 + **失败分类**（如 `test_failure` / `infra_flake` / `config_error`）
> → Agent 总结为"build#42 失败，原因是测试用例失败（非基础设施抖动），建议检查 xxx 提交"
> → **全程在 Langfuse 可见**（含 MCP 工具调用这一 span）
> **含金量**：这不是"我写了个 mock 工具"，而是"**我的 Agent 能诊断一家企业级 DevOps 平台上的真实流水线**"。

**场景 F（v4.0 新增）：Agentic BI 自主多步分析**
> 用户问"上个月哪个仓库的库存周转最慢？为什么？"
> → Agent 判断这是指标类问题 → 调用 `query_metric(metric="inventory_turnover", dimension="warehouse", ...)`
> → 工具从 `metrics.yaml` 读取指标口径，**用模板渲染 SQL**（LLM 全程不写 SQL）→ 返回 `西安 1.2 / 上海 3.8 / 成都 4.1`
> → Agent **自行判断**"西安最慢"，并**自主决定再深入一步**：`query_metric(metric="inventory_turnover", dimension="sku")`
> → 定位到西安某 SKU 出库量骤降
> → 前端 ECharts 渲染柱状图 + Agent 输出结论（含数据出处）
> **含金量**：①②两步之间**没有人类介入**——这就是"自主多步"，也是 Agentic BI 与上一代 ChatBI 的分界线。

**场景 G（v4.0 新增）：平台对照评测**
> 管理员用同一批 30 条问题，分别投给（a）本地 Dify 应用、（b）AgentForge → 两者答案交给**同一个 judge** 打分 → 生成对照报告：各自准确率、各自失败模式、结论 → 报告成为"为什么自建"的**实测证据**

---

## 6. 范围

### 6.1 In Scope（阶段 1 必须交付）

**原有 10 项（不变）**
1. LLM Gateway（DeepSeek + OpenAI 双通道，统一接口，Langfuse 埋点）
2. Agent 引擎（LangGraph ReAct，5 步上限，工具/检索决策）
3. RAG 引擎（解析 → 切片 → 向量化 → 混合检索 → 重排 → 引用生成）
4. 工具注册中心 + MCP 接入（FastMCP 库存服务 demo）
5. 记忆系统（滑窗 8 轮 + 20 轮摘要 + Redis/PG 持久化）
6. 评测体系（50 条评测集 + LLM-as-judge + 消融实验 + 报告）
7. Langfuse 可观测（LLM/检索/工具三层埋点）
8. Web UI（对话页 + 知识库管理 + 引用高亮 + 评测报告页）
9. 部署（Docker Compose：pgvector + redis + langfuse + api + web）
10. 测试（pytest 核心链路）+ 文档（README/架构图/演示脚本）

**v4.0 新增 4 项**
11. **外部 MCP Server 接入**：接入 Harness 官方 MCP Server（`harness-mcp-v2`），工具动态发现 + 注册 + 调用 + 生命周期管理
12. **MCP Server 管理**：`mcp_servers` 表持久化 + 重启自动重连 + toolset 裁剪 + 写操作风险分级
13. **Agentic BI 分析能力**：`metrics.yaml` 指标语义层 + `query_metric` 工具 + 多步分析 + 前端图表
14. **平台对照评测**：Dify 应用作为外部答案源接入 judge，产出对照报告

### 6.2 Out of Scope（阶段 2/3 或明确不做）

| 项 | 归属 |
|---|---|
| 多 Agent 编排（Supervisor/Planner-Executor） | 阶段 2 |
| Agent Runtime 平台化（状态持久化/多通道 Gateway/消息总线） | 阶段 3 |
| 管理后台完整版、可视化工作流画布 | 阶段 3 |
| 模型微调、知识图谱 | 明确不做 |
| 用户体系/计费/权限矩阵 | 明确不做（阶段 1 单用户） |
| **完整 BI 平台**（复杂图表体系、审计轨迹、多租户） | **明确不做**（v4.0 只做"给 Agent 加一类分析工具 + 一个语义层"） |
| **让 LLM 自由生成 SQL（NL2SQL）** | **明确不做**（v4.0 采用语义层模板渲染，见 §9.10） |
| **把 Dify 接成编排层/运行时依赖** | **明确不做**（会摧毁 §2.3 的自建叙事，且 Dify 检索是黑盒、无法支撑 S2 消融实验） |
| 用 Harness 承载 CI/CD 与评测门禁 | 不做（仓库在 Gitee 不在其集成清单；目标环境为 K8s 类，项目跑本机 compose。理由详见概念融合评估文档 §3.3） |

---

## 7. 功能需求详情

### F1 对话接口（P0）— 不变
- **F1.1** POST `/api/chat`：入参 `{session_id?, message, stream?}`，出参 SSE 流
- **F1.2** 支持流式（SSE）与非流式两种模式
- **F1.3** 会话不存在时自动创建，返回 session_id
- **F1.4** 每轮对话落库（messages 表），更新 session.updated_at
- **验收**：curl 发 3 轮对话，上下文正确；SSE 逐字返回

### F2 Agent 引擎（P0）— 不变
- **F2.1** LangGraph StateGraph：START → plan → (工具/检索 or 直接) → observe → ... → answer → END
- **F2.2** 状态：`messages[] + step_count + tool_results[]`，step 上限 5
- **F2.3** plan 节点注入工具 schema（JSON Schema），LLM 决定调用哪个工具
- **F2.4** 无工具调用时直接走 answer；工具异常时把错误信息返回给 LLM 重试 1 次
- **F2.5** 检索决策：LLM 判断"是否需要检索知识库"
- **验收**：CLI 脚本分别测"直答 / 调工具 / 检索"三条路径

> **v4.0 补充（F2.6）**：**多步分析的支持**——`step` 上限从"够用即可"改为**至少 5 步**，以满足场景 F 的"查指标 → 判断 → 再下钻"两步以上链式调用。工具结果需保留在 `tool_results[]` 中供后续步骤引用。

### F3 RAG 引擎（P0）— 不变
- **F3.1** 文档解析：PDF（pypdf）/ DOCX（python-docx）/ MD / TXT，按类型分发
- **F3.2** 切片：512 token + 10% 重叠；中文按字符近似；保留元信息（页码/标题路径）
- **F3.3** 向量化：embedding 模型经 LLM Gateway 统一调用，pgvector 存储
- **F3.4** 混合检索：BM25 关键词检索（**倒排索引用** PostgreSQL `tsvector` + GIN，**BM25 打分在应用层自算**）+ 向量（cosine）→ **RRF 融合**（k=60）
- **F3.5** 重排：bge-reranker-base（本地 ONNX 或 API），top20 → top5
- **F3.6** 检索策略可配置（`pure_vector` / `hybrid` / `hybrid_rerank`），供评测复用
- **F3.7** 引用定位：返回片段带 document_id + 页码/标题，答案生成强制标注 [n]
- **验收**：上传 md 文档，问 10 个问题，≥8 个引用可定位到原文

### F4 文档管理（P0）— 不变
- **F4.1** POST `/api/documents`（multipart），入库状态 processing → ready/failed
- **F4.2** GET / DELETE `/api/documents[/{id}]`
- **F4.3** 切片质量检查：空切片/超短切片告警（日志 + UI 状态）
- **验收**：上传/列表/删除全通；失败文档（坏 PDF）有 failed 状态

### F5 记忆系统（P0）— 不变（含已知欠账）
- **F5.1** 滑窗：最近 8 轮（user+assistant），超窗先摘要再丢弃
- **F5.2** 摘要：会话累计 >20 轮时，LLM 生成历史摘要，作为 system 前缀
- **F5.3** 存储：消息全量 PostgreSQL，热窗口 Redis（session_id 为 key，TTL 24h）
- **验收**：连续 5 轮上下文正确；模拟 21 轮后摘要生效且不丢失关键事实

> ⚠️ **欠账提醒**：截至 v4.0 编制日，D10（摘要记忆 + sessions API）**尚未实现**——`messages` / `sessions` 表实测 0 行，`memory_service.py` 无摘要逻辑。这导致对话历史目前仅有 Redis 单层（8 轮 + TTL 24h），**超 8 轮、超 24h 或 Redis 重启即永久丢失且无兜底**。补账优先级最高（详见 §15 风险表）。

### F6 工具注册中心 + MCP（P0）— **v4.0 扩展**

**原有条目（不变）**
- **F6.1** ToolRegistry：`name/description/parameters(schema)/handler` 四元组注册制
- **F6.2** 内置工具：`current_time`（时间/计算）、`web_search`（可选）
- **F6.3** MCP 客户端：官方 `mcp` Python SDK
- **F6.4** MCP demo 服务：`mcp_servers/inventory_server.py`（FastMCP，内存 mock 数据），暴露 `query_inventory(filters)`
- **F6.5** 工具调用失败不影响主流程
- **验收（原）**：对话中问"查库存数量小于 10 的商品"→ 走通 MCP 工具 → 返回 mock 数据

**v4.0 新增条目**

- **F6.6 外部 Server 注册**：`POST /api/mcp/register` 支持注册**任意标准 MCP Server**（不限于自研），入参扩展为 `{name, command, args, env, toolsets?}`。首个目标：Harness 官方 Server（`npx -y harness-mcp-v2@latest`）
- **F6.7 生命周期管理**：MCP Client 必须管理子进程全生命周期——启动、`initialize` 握手、健康检查、**崩溃自动重启（含退避）**、应用退出时清理。**防止 Node 子进程泄漏**
- **F6.8 Server 持久化**：`mcp_servers` 表记录已注册 server 的配置与状态，应用重启后**自动重连**，无需重复注册
- **F6.9 工具动态发现**：工具清单**运行时**从 server 的 `tools/list` 获取并注册（**不写死在代码中**）。新增 server 不需要改 Agent 代码
- **F6.10 toolset 裁剪**：支持只启用部分 toolset（Harness 有 41 个）。理由：**工具数量膨胀会降低 LLM 选工具的准确率**
- **F6.11 写操作风险分级**：工具按风险分级（`read` / `low_write` / `medium_write` / `high_write`），写操作需**显式确认**才执行。参考 Harness 的 `HARNESS_AUTO_APPROVE_RISK` 设计，阶段 1 默认**只读**
- **F6.12 凭据安全**：外部 server 的密钥（如 `HARNESS_API_KEY`）**只从 `.env` 读取**，不写入数据库、不写入代码、不打印日志
- **验收（新增）**：
  - 启动后 `/api/tools` 能列出 Harness 的 11 个工具（名称 + 描述 + 参数 schema）
  - 问"我最近哪些流水线失败了"→ Agent 选中 `harness_list` / `harness_diagnose` → 返回真实数据 → Langfuse 可见该 span
  - 手动 kill 子进程 → 客户端自动重启并恢复可用
  - 用只读 PAT 尝试写操作 → 被拦截并给出明确提示

### F7 评测体系（P0，差异化核心）— **v4.0 扩展**

**原有条目（不变）**
- **F7.1** 评测集：50 条，3 类
  - 类型 A 文档问答 30 条（含 5 条"文档中无答案"负例，测幻觉控制）
  - 类型 B 跨文档推理 10 条
  - 类型 C 工具调用 10 条
- **F7.2** LLM-as-judge 评分器：3 维度（正确性 / 引用忠实度 / 完整性），各 0–5 分
- **F7.3** 消融实验：同一评测集跑 3 配置（pure_vector / hybrid / hybrid_rerank）
- **F7.4** 报告生成：Markdown → `docs/eval-report-{date}.md`
- **F7.5** 准确率口径：judge 得分 ≥4/5 计为"对"
- **验收（原）**：一键跑完 3×50 条；报告含对比数据与失败案例

**v4.0 新增条目**

- **F7.6 评测集类型 D（多步分析）**：新增 **10 条**，专测场景 F——每个用例需 **≥2 次工具调用**才能得出正确答案。评测集总数 **50 → 60 条**
  - 判定标准：不只判"答案对不对"，还要判**工具调用序列是否合理**（是否真的做了下钻、是否选对指标与维度）
- **F7.7 外部答案源**：judge 支持接收"外部系统答案"作为独立列，用于 D37 对照评测（Dify 应用答案 vs AgentForge 答案，**同一 judge、同一评分口径**）
- **F7.8 对照报告**：产出 `docs/eval-compare-{date}.md`，含：两系统总分对比、分类型对比、**失败模式差异分析**（Dify 的黑盒检索失败模式 vs 自研可控链路的失败模式）
- **F7.9 安全用例**：评测集内嵌 SQL 注入测试用例（`dimension` 参数注入、指标名注入），要求 **100% 被拦截**

### F8 Langfuse 可观测（P0）— 不变
- **F8.1** 部署：Langfuse Cloud（免费额度）或 Docker 自托管
- **F8.2** 埋点：LLM 调用、检索、工具（入参出参）、Agent 轨迹（trace/span）
- **F8.3** UI 展示：会话追踪树、token 消耗、延迟分析
- **验收**：一次"带工具调用的对话"在面板中完整可见全链路

> **v4.0 补充（F8.4）**：**MCP 工具调用必须单独可辨识**——span 元数据需含 `mcp_server_name` 与 `tool_name`，以便演示时能明确指出"这一次是 Harness 外部 server 的调用，不是自研工具"。

### F9 Web UI（P0，React + TS + Vite）— **v4.0 扩展**

**原有条目（不变）**
- **F9.1** 对话页（ChatPage）：消息流、SSE 流式展示（打字机效果）、引用高亮（Citation 组件点击展开原文）、新建会话
- **F9.2** 知识库页（KnowledgePage）：上传、进度状态、文档列表、删除
- **F9.3** 评测页（EvalPage）：触发评测、轮询进度、渲染 Markdown 报告
- **F9.4** 侧边栏：模型选择（deepseek/openai）、会话历史
- **F9.5** 技术要点：axios + fetch 处理 SSE；vite dev proxy → :8000；Ant Design 组件库
- **验收（原）**：浏览器完整走通场景 A/B/C/D；打字机流式 + 引用高亮可用

**v4.0 新增条目**

- **F9.6 工具调用可视化**：对话中工具调用需以**独立卡片**呈现（工具名 / 来源 server / 入参 / 返回摘要 / 耗时），可折叠展开。目的：让"Agent 调了外部系统"这件事**在 UI 上可见**，而不只在 Langfuse 里可见
- **F9.7 分析页（AnalyticsPage）**：自然语言提问 → Agent 多步分析 → **ECharts 图表渲染**（柱状/折线/趋势）。关键：`query_metric` 返回**结构化数据（dict）而非字符串**，前端直接消费
- **F9.8 MCP Server 管理面板**：列出已注册 server、连接状态、工具数量、启用/禁用（P1 可降级为只读展示）
- **验收（新增）**：浏览器内问"哪个仓库周转最慢"→ 出图 + 结论；对话内工具卡片能显示"来源：Harness"

### F10 部署与测试（P0）— 不变
- **F10.1** docker-compose.yml：pgvector + redis + langfuse + api + web
- **F10.2** pytest：核心链路（LLM 直答/工具/RAG 检索/记忆/评测 API）
- **F10.3** README（架构图 + 启动指南 + 演示脚本）
- **验收**：全新环境 docker compose up 后 10 分钟可演示

> **v4.0 补充（F10.4）**：**Node 运行时依赖**——Harness MCP Server 是基于 Node 的子进程，Docker 镜像需包含 Node（或文档中明确宿主要求）。同时**首次启动预热**必须写入启动脚本（`npx` 拉包 + 可选下载约 23MB ONNX 模型），否则演示首问会卡顿。

### F11 Agentic BI 分析能力（P0，v4.0 新增）★

> 完整设计见 §9.10。**核心技术决策：LLM 不写 SQL，只选指标名 / 维度 / 时间范围，SQL 由语义层模板渲染。**

- **F11.1 指标语义层**：`metrics.yaml` 定义 ≥ 3 个指标（含 ≥ 1 个用于下钻的明细指标），每项含 `label / description / table / time_column / dimensions（白名单）/ sql_template`
- **F11.2 语义层加载与渲染**：`metrics_service.py` 负责加载 YAML、校验指标与维度白名单、按模板渲染 SQL、参数化绑定时间范围
- **F11.3 查询工具**：`query_metric(metric, dimension?, start_date, end_date)` 注册进 ToolRegistry，**返回结构化 dict 而非字符串**（供前端图表直接消费）
- **F11.4 三层防护**：① 指标名白名单 ② 维度白名单（`{dimension}` 只能取 `dimensions` 中的值）③ 时间走参数绑定。三者缺一不可
- **F11.5 多步分析编排**：Agent 允许在拿到初步结果后自主发起第二轮下钻调用；`analysis_trace` 记录完整调用序列（供 F7.6 判定 `sequence_ok`）
- **F11.6 分析页**：`AnalyticsPage` + `MetricChart`（ECharts），渲染柱状/折线图，并展示 Agent 结论与数据出处
- **验收**：
  - `GET /api/metrics` 能列出全部指标定义
  - 问"上个月哪个仓库周转最慢？为什么"→ **两次工具调用**（排名 → 下钻）→ 出图 + 结论
  - SQL 注入用例（维度注入 / 指标名注入 / 时间参数注入）**100% 被拦截**
  - 不存在的指标或维度 → 返回明确错误，不落到数据库

### P1（有余力）
- P1-1 JWT 简单鉴权
- P1-2 管理页雏形（模型/工具配置）
- P1-3 更多 MCP 工具
- P1-4 Redis 相似问题缓存
- **P1-5（新增）** 更丰富的指标与图表类型（饼图/热力图/多维下钻）
- **P1-6（新增）** Dify 作为 MCP Server 反向挂载（Dify 1.17 已内置服务端实现，源码已证实可行）

---

## 8. 技术架构

### 8.1 总体架构图（v4.0 更新）

```
┌───────────────────────────────────────────────────────────────┐
│                    Web UI（React + TS + Vite）                  │
│  对话页 ｜ 知识库 ｜ 引用高亮 ｜ 评测报告 ｜ 分析页(图表) ｜ 工具卡片 │
└──────────────────────────┬────────────────────────────────────┘
                           │ HTTP/SSE
┌──────────────────────────▼────────────────────────────────────┐
│                  API 层（FastAPI + Pydantic v2）                │
│   /chat /documents /sessions /eval /tools /mcp /metrics /health │
└──────────────────────────┬────────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────────┐
│              Agent 引擎（LangGraph ReAct，支持多步）★            │
│   plan → [tool?/rag?] → execute → observe → ... → answer       │
│        │                 │                                     │
│        │                 ▼                                     │
│        │      ┌──────────────────────────────────────┐          │
│        │      │         Tool Registry ★              │          │
│        │      │  current_time ｜ query_inventory     │          │
│        │      │  query_metric（Agentic BI）🆕        │          │
│        │      └───────┬──────────────────┬───────────┘          │
│        │              │                  │                      │
│        │      ┌───────▼────────┐  ┌──────▼──────────────┐       │
│        │      │ MCP Client ★   │  │ 本地工具（进程内）    │       │
│        │      │ 动态发现+生命周期│  │ analytics_service   │       │
│        │      └───┬────────┬───┘  └──────┬──────────────┘       │
│        ▼          │        │             │                      │
│  ┌──────────────┐ │        │      ┌──────▼──────────────┐       │
│  │ RAG Engine ★ │ │        │      │ metrics.yaml 🆕      │       │
│  │ 混合检索+重排  │ │        │      │ 指标语义层（口径）    │       │
│  └──────────────┘ │        │      └─────────────────────┘       │
│  ┌──────────────┐ │        │                                    │
│  │ Memory Svc   │ │        │                                    │
│  │ 滑窗+摘要     │ │        │                                    │
│  └──────────────┘ │        │                                    │
│  ┌──────────────┐ │        │                                    │
│  │ Eval Service │ │        │                                    │
│  │ 评测/消融/对照│ │        │                                    │
│  └──────────────┘ │        │                                    │
│        │          │        │                                    │
│        ▼          ▼        ▼                                    │
│  ┌───────────────────────────────────────────────────────┐     │
│  │        LLM Gateway ★（DeepSeek/OpenAI 双通道）          │     │
│  └───────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
        │          │        │              │              │
        ▼          ▼        ▼              ▼              ▼
   PostgreSQL   Redis   Langfuse   自研 MCP Server   Harness MCP
   (pgvector+   (会话   (可观测)    inventory_server  Server 🆕
    tsvector)   缓存)                （Node 子进程）
                                    （Python）        → Harness 云端 API
                                                     Dify App 🆕（可选，P1-6）
```

**图例**：🆕 = v4.0 新增

### 8.2 分层架构与扩展点（阶段 2/3 零返工前提）— v4.0 更新

| 层 | 职责 | 阶段 1 实现 | 阶段 2/3 复用 |
|---|---|---|---|
| **LLM Gateway** | 所有模型调用唯一入口 | DeepSeek/OpenAI 双通道 + 埋点 | 阶段 3 加多 provider/降级/计费 |
| **Tool Registry** | 工具注册制管理 | 内置 2 + 自研 MCP 1 + **外部 MCP 11** | 阶段 2 多 Agent 挂不同工具集 |
| **MCP Client** 🆕 | 外部能力接入层 | 动态发现 + 生命周期 + 持久化 | 阶段 3 多通道 Gateway 的基础 |
| **Agent 引擎** | LangGraph 图执行 | 单 Agent ReAct（多步） | 阶段 2 嵌为子图 |
| **RAG Engine** | 检索链路 | 混合+重排+配置化 | 不变，多 Agent 共用 |
| **Metrics 语义层** 🆕 | 指标口径唯一来源 | `metrics.yaml` + 模板渲染 | 阶段 2 多 Agent 共享同一口径 |
| **Eval Service** | 评测 | 评测集+judge+消融+**对照** | 阶段 2 评测多 Agent 轨迹 |
| **可观测** | 追踪 | Langfuse 四层埋点（含 MCP） | 阶段 3 管理后台消费数据 |

**红线（不变，v4.0 追加 3 条）**：
1. 业务代码不得绕过 Gateway 直连 SDK
2. Agent 不得直接调函数（必须经 Registry）
3. 评测模块与业务解耦（输入配置+评测集，输出报告）
4. **🆕 任何外部凭据不得进入代码/数据库/日志，只从 `.env` 读**
5. **🆕 MCP 工具不得写死在代码中，必须运行时动态发现**
6. **🆕 结构化数据查询不得由 LLM 直接生成 SQL，必须经语义层模板渲染**

### 8.3 目录结构（目标）— v4.0 更新（🆕 为新增）

```
AgentForge-Lite/
├── app/
│   ├── main.py                 # FastAPI 入口、路由注册、异常处理
│   ├── config.py               # pydantic-settings（.env 驱动）
│   ├── api/
│   │   ├── chat.py             # POST /api/chat
│   │   ├── documents.py        # 文档上传/列表/删除
│   │   ├── sessions.py         # 会话管理
│   │   ├── eval.py             # 评测 API
│   │   ├── tools.py            # 工具列表/注册
│   │   ├── mcp.py          🆕  # MCP Server 管理（注册/列表/启停）
│   │   ├── metrics.py      🆕  # 指标语义层查询
│   │   └── health.py           # 健康检查
│   ├── core/
│   │   ├── db.py               # async engine + session
│   │   ├── redis.py            # Redis 客户端
│   │   └── logging.py          # 结构化日志
│   ├── models/                 # SQLAlchemy ORM
│   │   ├── session.py
│   │   ├── message.py
│   │   ├── document.py
│   │   ├── chunk.py
│   │   ├── eval.py
│   │   └── mcp_server.py   🆕  # 已注册 MCP Server
│   ├── schemas/                # Pydantic 模型
│   │   ├── chat.py
│   │   ├── document.py
│   │   ├── eval.py
│   │   ├── mcp.py          🆕
│   │   ├── metric.py       🆕
│   │   └── common.py
│   ├── services/
│   │   ├── llm_gateway.py      # ★ LLM 统一入口 + 埋点
│   │   ├── agent_service.py    # ★ LangGraph 编排（多步）
│   │   ├── rag_service.py      # ★ 混合检索+重排
│   │   ├── memory_service.py   # 滑窗+摘要
│   │   ├── eval_service.py     # ★ 评测/消融/对照/报告
│   │   ├── document_service.py # 解析/切片/入库
│   │   └── metrics_service.py 🆕 # ★ 语义层加载 + SQL 模板渲染 + 白名单校验
│   ├── tools/
│   │   ├── registry.py         # ★ 工具注册中心（含风险分级）
│   │   ├── current_time.py
│   │   ├── mcp_client.py       # ★ MCP 客户端（动态发现 + 生命周期）
│   │   ├── inventory.py        # 自研 MCP 库存工具包装
│   │   ├── harness.py      🆕  # Harness 外部工具包装（只读工具集）
│   │   └── analytics.py    🆕  # ★ query_metric 工具（Agentic BI 入口）
│   └── prompts/                # 所有 prompt 集中管理
│       ├── system.py
│       ├── rag.py
│       ├── summarizer.py
│       ├── judge.py
│       ├── analytics.py    🆕  # 多步分析引导 prompt
│       └── mcp_tools.py    🆕  # 外部工具使用约束 prompt
├── mcp_servers/
│   └── inventory_server.py     # FastMCP 库存服务（自研 demo）
├── metrics.yaml            🆕  # ★ 指标语义层（口径唯一来源）
├── tests/
│   ├── test_chat.py
│   ├── test_agent.py
│   ├── test_rag.py
│   ├── test_memory.py
│   ├── test_eval.py
│   ├── test_mcp_client.py  🆕  # 生命周期/重启/动态发现
│   ├── test_analytics.py   🆕  # 语义层/白名单/SQL 注入拦截
│   └── conftest.py
├── frontend/                    # React + TS + Vite（用户主战场）
│   ├── src/
│   │   ├── App.tsx
│   │   ├── api/
│   │   │   ├── client.ts        # axios 封装（SSE 处理）
│   │   │   ├── chat.ts
│   │   │   ├── documents.ts
│   │   │   ├── eval.ts
│   │   │   ├── mcp.ts      🆕
│   │   │   └── metrics.ts  🆕
│   │   ├── pages/
│   │   │   ├── ChatPage.tsx     # 对话页（流式/引用高亮/工具卡片）
│   │   │   ├── KnowledgePage.tsx# 知识库管理
│   │   │   ├── EvalPage.tsx     # 评测报告（含对照报告）
│   │   │   └── AnalyticsPage.tsx 🆕 # ★ 分析页（ECharts）
│   │   ├── components/
│   │   │   ├── MessageList.tsx
│   │   │   ├── Citation.tsx     # 引用高亮组件
│   │   │   ├── ChatInput.tsx
│   │   │   ├── ToolCallCard.tsx 🆕 # 工具调用卡片（含来源 server）
│   │   │   └── MetricChart.tsx  🆕 # ★ ECharts 封装
│   │   └── styles/
│   ├── package.json
│   ├── vite.config.ts           # dev proxy → :8000
│   └── tsconfig.json
├── scripts/
│   ├── eval_run.py             # 命令行跑评测
│   ├── eval_compare.py     🆕  # ★ D37 对照评测（Dify vs AgentForge）
│   ├── dify_bridge.py      🆕  # Dify 应用调用适配（外部答案源）
│   ├── warmup_mcp.py       🆕  # MCP Server 预热（演示前必跑）
│   └── seed_demo_data.py       # 示例文档+评测集
├── data/
│   └── sample_docs/            # 演示文档
├── docs/
│   ├── PRD.md                  # v3.0（原版，保留）
│   ├── PRD-v4-含前沿技术.md      # ★ 本文件
│   ├── 概念融合评估-2026-09-21.md # 可行性分析 v4
│   ├── eval-report-{date}.md   # 评测报告
│   ├── eval-compare-{date}.md  # 🆕 对照报告
│   └── architecture.md
├── docker-compose.yml
├── Dockerfile                  # 🆕 需含 Node 运行时
├── .env.example
├── .gitignore
├── pyproject.toml
├── requirements.txt            # 🆕 显式依赖清单
└── README.md
```

### 8.4 技术选型与理由 — v4.0 补充

| 技术 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | JD 主流；uv 管理 |
| Web 框架 | FastAPI | 异步、Pydantic v2 集成、自动 OpenAPI |
| Agent | LangGraph | 状态机图、checkpoint、stream、可演进 |
| LLM 接入 | DeepSeek + OpenAI | 双通道降级；JD 高频 |
| 向量库 | pgvector | 少一个中间件，Docker 一条命令 |
| BM25 | 自算打分（倒排索引用 `tsvector` + GIN） | 免 ES 重量级组件。PG 内建 `ts_rank` **不是** BM25 |
| 重排 | bge-reranker-base | 中文效果好，可本地/API |
| 缓存 | Redis | 会话热窗口、后续缓存 |
| 可观测 | Langfuse | 海外远程岗点名；开源自托管 |
| MCP | 官方 mcp SDK + FastMCP | 标准协议，JD 必考 |
| **MCP Server（外部）** 🆕 | **Harness 官方 `harness-mcp-v2`** | **真实企业系统；官方开源；MIT；11 工具 × 252 资源类型** |
| **运行时（外部 Server）** 🆕 | **Node 22+（`npx`）** | Harness MCP Server 的运行时要求。对纯 Python 项目是一次受控引入 |
| **指标语义层** 🆕 | **YAML + Jinja 风格模板** | 无新框架依赖；可读、可 review、可版本化 |
| **图表** 🆕 | **ECharts（echarts-for-react）** | 用户前端主力；中文生态完善 |
| UI | React + TS + Vite + Ant Design | 用户主战场，全栈真实感 |
| 测试 | pytest + pytest-asyncio | 标准 |
| 部署 | Docker Compose | 一键演示 |

---

## 9. 模块详细设计

### 9.1 LLM Gateway — 不变
- **通道配置**（`.env`）：`LLM_PROVIDER=deepseek|openai`，`DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
- **双通道**：主通道失败（超时/4xx/5xx）→ 备用通道重试 1 次
- **场景参数**：规划 temp=0.2、回答 temp=0.4、摘要 temp=0.1、评测 judge temp=0、**🆕 分析引导 temp=0.2**
- **Langfuse 埋点**：每个 span 记录 model/input/output/token/耗时
- **关键设计决策**：所有 LLM 调用必须经 gateway——阶段 3 加多 provider 不返工的前提

### 9.2 Agent 引擎（LangGraph）— v4.0 补充多步支持

```
class AgentState(TypedDict):
    messages: list[dict]        # 完整对话（含工具消息）
    step_count: int
    tool_results: list[dict]
    analysis_trace: list[dict]  # 🆕 多步分析轨迹（供评测判定调用序列）
```

图结构不变，但 `should_answer` 的判定条件放宽：**当 agent 处于"分析型任务"且已获得部分结论时，允许主动再发起一轮工具调用**（场景 F 的下钻）。

- `recursion_limit` = 10（plan 最多跑 5 次）
- **🆕 多步分析引导**：`prompts/analytics.py` 中明确指示——"若初步结果指向某个异常对象（如最慢的仓库），应主动下钻一层以给出原因，而非止步于排名"

### 9.3 RAG 引擎 — 不变（含重要术语澄清）

**检索流程**：
```
query → 向量检索（pgvector cosine top20）
      → BM25 关键词检索（倒排走 tsvector/GIN，BM25 分在应用层自算；top20）
      → RRF 融合（k=60）→ top20
      → bge-reranker 重排 → top5
      → 组装 prompt（片段+引用编号）
```

**RRF 公式**：`score(d) = Σ 1/(k + rank_i(d))`，k=60

**术语澄清（面试话术的源头，别混用）**：

- `tsvector` 是**索引结构**（倒排表的物理形态），BM25 是**打分公式** —— 两者不是一回事，不能写成「BM25（PostgreSQL tsvector）」
- PG 内建 `ts_rank(tsvector, tsquery)` **不是 BM25**：它只接收这两个入参，拿不到 `df` / `N` / `avgdl`，**IDF 在数学上就无法计算**；且默认参数下没有文档长度归一化（实测：同 TF 下 3 词短文档与 16 词长文档得分完全相同 `0.060793`）
- 本项目做法：**倒排索引用 `tsvector` + GIN，BM25 打分在应用层自算**（`N` / `df` / `avgdl` 用 CTE 现算，量大时改维护统计表）。已在 PG16 实测可行，参考实现见 `scripts/d13_probe_retrieval.py`
- **中文分词**：PG 内置分词器对中文无效（整句退化成一个 token，实测查「智能」不命中），故采用**应用层 jieba 分词 + `simple` 配置**；⚠️ 索引侧与查询侧**必须用同一分词器**，否则 token 对不上、检索静默变差（需加一致性自检断言兜底）

**切片策略**：512 token + 10% 重叠；中文按字符（~750 字符）近似；保留 `page_ref` 与标题路径用于引用定位。
**检索策略配置**：`RETRIEVER_CONFIG = pure_vector | hybrid | hybrid_rerank`

### 9.4 记忆系统 — 不变
- 热窗口：Redis `session:{id}:window`，存最近 8 轮
- 冷数据：PostgreSQL messages 全量
- 摘要触发：轮数 >20 → `summarizer` prompt 生成摘要 → 存 session.summary → 作为 system 前缀
- 组装顺序：`system(人设+摘要) → 窗口内消息`

### 9.5 评测体系 — v4.0 扩展

**评测集格式**（新增 `multi_step` 类别）：
```json
{
  "id": 1,
  "category": "doc_qa|cross_doc|tool_call|multi_step",
  "question": "...",
  "reference": "参考答案",
  "doc_ids": ["uuid"],
  "expected_tool": "query_inventory",
  "expected_call_sequence": ["query_metric", "query_metric"]
}
```

**Judge 评分**（不变，3 维度 0–5 分）+ **🆕 多步题追加判定**：
```
若 category=multi_step，额外判定：
4. 调用序列合理性：是否真的做了下钻（而非一次调用后臆测结论）
输出 JSON: {"correctness": n, "faithfulness": n, "completeness": n, "sequence_ok": true|false}
```

**消融实验矩阵**（不变）：

| 配置 | 检索 | 重排 |
|---|---|---|
| A. pure_vector | 仅向量 top5 | 无 |
| B. hybrid | BM25+向量 RRF top5 | 无 |
| C. hybrid_rerank | BM25+向量 RRF top20 | bge-reranker top5 |

**报告结构**：总体对比表 → 每类明细 → 失败案例（各取 3 条）→ 结论与建议。
**🆕 对照报告结构**（F7.8）：两系统总分 → 分类型对比 → 失败模式差异 → 成本/可控性权衡结论。

### 9.6 MCP 接入 — v4.0 大幅扩展

#### 9.6.1 自研 Server（保留，理解协议两端）

**mcp_servers/inventory_server.py**（FastMCP）：
```python
from fastmcp import FastMCP
mcp = FastMCP("inventory")

@mcp.tool()
def query_inventory(min_stock: int | None = None, category: str | None = None) -> list[dict]:
    """查询库存商品。min_stock: 库存下限过滤; category: 品类过滤"""
    # mock 数据：返回商品列表
```
- 运行：`python mcp_servers/inventory_server.py`（stdio）
- 客户端：`mcp_client.py` 连接 → 动态发现工具 → 注册进 ToolRegistry

#### 9.6.2 外部 Server 接入（v4.0 新增）★

**目标**：接入 Harness 官方 MCP Server。

**核心事实**（来源：`github.com/harness/mcp-server` README，2026-09-22 读取）：

| 项 | 内容 |
|---|---|
| 包 / 许可 | npm `harness-mcp-v2`（MIT） |
| 规模 | **11 个工具 × 252 种资源类型**，41 个 toolsets，35 个 prompt 模板 |
| 工具 | `harness_list / get / create / update / delete / execute / search / diagnose / status / describe / schema` |
| 起法 | `HARNESS_API_KEY=pat.xxx npx -y harness-mcp-v2@latest`（stdio 默认） |
| 认证 | PAT，格式 `pat.<accountId>.<tokenId>.<secret>`（account id 自动提取） |
| 写操作护栏 | `HARNESS_AUTO_APPROVE_RISK` = none / low_write / medium_write / high_write / all |
| 诊断能力 | `harness_diagnose` 附 6 类失败分类：infra_flake / test_failure / config_error / dependency_failure / permission_error / timeout |

> ⚠️ **规模数字会变**：该仓库 commit 频率接近每日，引用前须以 README 当日口径为准。

**注册流程**（走 PRD 已有的 `POST /api/mcp/register`）：

```bash
curl -X POST http://localhost:8000/api/mcp/register \
  -H 'Content-Type: application/json' \
  -d '{"name": "harness",
       "command": "npx",
       "args": ["-y", "harness-mcp-v2@latest"],
       "env": {"HARNESS_API_KEY": "pat.xxx.yyy.zzz"},
       "toolsets": ["pipeline", "execution"]}'
```

**MCP Client 内部动作**（此段是 D27 原计划内容，非为 Harness 新写）：
1. `subprocess` 启动子进程
2. stdio 发 `initialize` 握手（协议版本协商）
3. 发 `tools/list` 取回工具清单
4. 逐个 `ToolRegistry.register(name, description, schema, handler, risk_level)`
5. 子进程常驻；Agent 触发调用时把 LangGraph 的 `tool_call` 翻译成 JSON-RPC `tools/call`

**四个必须处理的工程细节**：

| # | 细节 | 对策 |
|---|---|---|
| 1 | stdio = 常驻子进程，非 HTTP | 实现生命周期管理：启动/健康检查/**崩溃重启（退避）**/退出清理，防子进程泄漏 |
| 2 | 首次启动慢（拉 npm 包 + 可选下载 ~23MB ONNX 模型） | `scripts/warmup_mcp.py` 预热；**锁版本**；演示前必跑 |
| 3 | 写操作风险分级 | 阶段 1 默认只读（`none` 或 `low_write`）；F6.11 显式确认门 |
| 4 | 41 个 toolset 全开会塞满上下文 | 按需裁剪（F6.10）。官方理由：**工具数膨胀降低 LLM 选型准确率** |

### 9.7 Web UI（React + TS + Vite）— v4.0 扩展

- **脚手架**：Vite + React + TypeScript + Ant Design
- **页面**：`ChatPage`、`KnowledgePage`、`EvalPage`、🆕 `AnalyticsPage`
- **SSE 流式**：fetch + ReadableStream 逐块解析，打字机效果
- **引用高亮**：`Citation` 组件，点击 `[n]` 弹出原文片段
- **🆕 工具调用卡片**：`ToolCallCard` —— 展示工具名、**来源 server**、入参、返回摘要、耗时
- **🆕 图表**：`MetricChart` —— echarts-for-react 封装，消费 `query_metric` 返回的结构化数据
- **API 对接**：axios 封装，`vite.config.ts` 配 dev proxy 转发到 `:8000`

### 9.8 部署 — v4.0 补充

**docker-compose.yml 服务**：
| 服务 | 镜像 | 端口 | v4.0 变更 |
|---|---|---|---|
| db | pgvector/pgvector:pg16 | 5432 | — |
| redis | redis:7-alpine | 6379 | — |
| langfuse | langfuse/langfuse:latest | 3000 | — |
| api | 本地构建 | 8000 | 🆕 Dockerfile 需含 Node 运行时 |
| web | node:22 构建 + nginx 托管前端 | 80 | — |

**health 检查**：`GET /health` 返回各依赖连接状态。
**🆕 启动预热**：compose 的 api 服务启动脚本中串入 `warmup_mcp.py`（或提供 `make warmup`），避免演示首问卡顿。

### 9.9 外部 MCP Server 接入（v4.0 新增）★

见 §9.6.2。**关键设计原则：接入新 server 不需要修改 Agent 代码**——这是"动手册"能力的证明。

### 9.10 Agentic BI 语义层（v4.0 新增）★

#### 9.10.1 核心设计决策（本版最重要的技术观点）

> **LLM 不写 SQL。LLM 只选三样：指标名、维度、时间范围。SQL 由 `metrics.yaml` 的模板渲染。**

这一条直接消掉了 Agentic BI 最大的翻车点（模型生成的 SQL 跑错/跑废/跑出越权查询）。
**面试被问"你怎么保证 NL2SQL 不出错"，最强答案不是"我加了校验"，而是"我没让它写 SQL"。**

#### 9.10.2 工具实现（`app/tools/analytics.py`）

```python
@tool
def query_metric(metric: str, dimension: str | None = None,
                 start_date: str = "", end_date: str = "") -> dict:
    """按口径查询业务指标。可用指标名与维度见 metrics.yaml。

    Args:
        metric: 指标名，如 inventory_turnover
        dimension: 分组维度，如 warehouse / sku
        start_date / end_date: YYYY-MM-DD
    """
    spec = METRIC_REGISTRY[metric]                       # 语义层加载
    if dimension and dimension not in spec.dimensions:
        raise ValueError(f"不支持的维度: {dimension}")    # 白名单校验 → 注入防护
    sql = render(spec.sql_template, dimension=dimension)  # 模板渲染，非自由生成
    rows = db.fetch_all(sql, {"start": start_date, "end": end_date})
    return {"metric": metric, "dimension": dimension, "rows": rows}   # 结构化返回
```

**三层防护**：
1. **指标名白名单** —— 不在 `METRIC_REGISTRY` 中直接拒绝
2. **维度白名单** —— 不在 `spec.dimensions` 中直接拒绝（`{dimension}` 只能取白名单值）
3. **时间参数绑定** —— 走参数化查询，不做字符串拼接

#### 9.10.3 语义层文件（`metrics.yaml`，约 20–40 行）

```yaml
metrics:
  inventory_turnover:
    label: 库存周转率
    description: 出库量 / 平均库存，衡量库存健康度
    table: inventory_movements
    time_column: occurred_at
    dimensions: [warehouse, sku, category]      # 白名单：注入防护的依据
    sql_template: |
      SELECT {dimension} AS dim,
             SUM(qty_out)::float / NULLIF(AVG(qty_on_hand), 0) AS value
      FROM inventory_movements
      WHERE occurred_at >= :start AND occurred_at < :end
      GROUP BY {dimension}
      ORDER BY value DESC

  inventory_flow:
    label: 出入库明细
    description: 一定时间内的入库量/出库量，用于下钻分析原因
    table: inventory_movements
    time_column: occurred_at
    dimensions: [warehouse, sku]
    sql_template: |
      SELECT {dimension} AS dim,
             SUM(qty_in) AS inbound, SUM(qty_out) AS outbound
      FROM inventory_movements
      WHERE occurred_at >= :start AND occurred_at < :end
      GROUP BY {dimension}

  stock_level:
    label: 当前库存水位
    description: 各对象的当前库存量
    table: inventory_snapshot
    time_column: snapshot_at
    dimensions: [warehouse, sku]
    sql_template: |
      SELECT {dimension} AS dim, qty_on_hand AS value
      FROM inventory_snapshot
      WHERE snapshot_at >= :start
```

> **语义层是 Agentic BI 的公认前置条件**——它是"业务口径"的唯一来源，也是 agent 与数据库之间的一层翻译。

#### 9.10.4 多步分析编排（场景 F 的实现）

```
用户："上个月哪个仓库的周转最慢？为什么？"
 ① plan 节点 → 选 query_metric(inventory_turnover, dimension=warehouse)
 ② observe → 得到 [西安 1.2, 上海 3.8, 成都 4.1]
 ③ plan 节点（第二轮）→ LLM 判断："西安最慢，需要下钻原因"
    → 选 query_metric(metric=inventory_flow, dimension=sku)
 ④ observe → 定位到西安某 SKU 出库量骤降
 ⑤ answer → 结论 + 图表数据 + 数据出处
```

**①②与③④之间没有人类介入**——这就是"自主多步"。`analysis_trace` 记录完整调用序列，供评测（F7.6 `sequence_ok`）判定。

### 9.11 平台对照评测（v4.0 新增）

**目的**：把 §2.3"为什么自建"从论证表变成实测数据。

**流程**：
```
30 条同题（跨 A/B/C/D 类）
   ├── AgentForge 作答（可配置检索链路）
   └── Dify 应用作答（走已发布的 Dify API）
        ↓
   同一个 judge（同 prompt、同口径、temp=0）
        ↓
   docs/eval-compare-{date}.md
```

**Dify 侧调用链**（已验证可用）：`X-App-Code` → `/api/passport` → `X-App-Passport` → `/api/chat-messages`

**报告要回答的问题**：
1. 两者总体准确率差异多少？
2. 差异集中在哪一类问题？（预期：跨文档推理与多步分析类差距最大——因为 Dify 检索黑盒、无 RAG 配置可调）
3. 各自的失败模式是什么？（Dify：检索召回失败且无法定位原因；AgentForge：可控，可做消融归因）
4. 成本对比：自建的复杂度代价 vs 可控性收益

> ⚠️ **前提**：必须先有评测体系（D20–D24）。没有 judge 与评测集，这件事做不了。

---

## 10. 数据模型

**v4.0 仅新增 1 张表**，其余沿用 v3.0。

```sql
-- ↓↓↓ 原有表（不变）：sessions / messages / documents / document_chunks / eval_cases / eval_runs ↓↓↓
-- 说明：定义见 docs/PRD.md §10。三条关键约束保留：
--   1) document_chunks 的倒排行用 tsvector + GIN，BM25 打分在应用层自算
--   2) 分词列 content_tokens 由 jieba 生成，索引侧与查询侧必须同一分词器
--   3) embedding 用 hnsw + vector_cosine_ops

-- 评测用例：category 增加 multi_step，并新增调用序列字段
ALTER TABLE eval_cases ADD COLUMN expected_call_sequence JSONB;   -- 🆕 ['query_metric','query_metric']
-- category 取值扩展为：doc_qa | cross_doc | tool_call | multi_step

-- 🆕 已注册的 MCP Server（v4.0 新增）
CREATE TABLE mcp_servers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,            -- 如 'harness' / 'inventory'
    is_builtin BOOLEAN DEFAULT false,     -- 自研 or 外部
    command TEXT NOT NULL,                -- 如 'npx'
    args JSONB NOT NULL,                  -- ["-y","harness-mcp-v2@latest"]
    env_keys JSONB,                       -- 只存「键名」，绝不存值（铁律 4）
    toolsets JSONB,                       -- 裁剪后的 toolset 白名单
    risk_policy TEXT DEFAULT 'read_only', -- read_only / low_write / ...
    status TEXT DEFAULT 'disconnected',   -- connected / disconnected / error
    tool_count INT DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

> 🔒 **安全红线**：`env_keys` 只存键名（如 `["HARNESS_API_KEY"]`），**值永远只从 `.env` 读取**。这是 §8.2 红线 4 的落地方式。

---

## 11. API 设计

**原有接口（不变）**

| 方法 | 路径 | 说明 | 请求 | 响应 |
|---|---|---|---|---|
| POST | /api/chat | 对话（流式/非流式） | `{session_id?, message, stream?}` | SSE 或 `{session_id, reply}` |
| GET | /api/sessions | 会话列表 | - | `[{id,title,updated_at}]` |
| GET | /api/sessions/{id}/messages | 会话历史 | - | `[{role,content}]` |
| POST | /api/documents | 上传文档 | multipart | `{id,status}` |
| GET | /api/documents | 文档列表 | - | `[{id,filename,status,chunk_count}]` |
| DELETE | /api/documents/{id} | 删除（级联切片） | - | 204 |
| GET | /api/tools | 已注册工具 | - | `[{name,description,source,risk_level}]` |
| GET | /api/eval/cases | 评测集 | - | `[{id,category,question}]` |
| POST | /api/eval/cases | 添加用例 | `{category,question,reference}` | `{id}` |
| POST | /api/eval/run | 触发评测 | `{config?}` | `{run_id}` |
| GET | /api/eval/runs/{id} | 评测结果 | - | `{config,accuracy,report_path}` |
| GET | /health | 健康检查 | - | `{status:"ok",deps:{...}}` |

**v4.0 变更与新增**

| 方法 | 路径 | 说明 | 请求 | 响应 |
|---|---|---|---|---|
| POST | /api/mcp/register | 注册 MCP 服务（**入参扩展**） | `{name, command, args, env, toolsets?}` | `{tools:[...]}` |
| GET | /api/mcp/servers | 🆕 已注册 server 列表 | - | `[{name,status,tool_count,is_builtin}]` |
| POST | /api/mcp/servers/{id}/reconnect | 🆕 重连 | - | `{status}` |
| DELETE | /api/mcp/servers/{id} | 🆕 注销并清理子进程 | - | 204 |
| GET | /api/metrics | 🆕 指标语义层列表 | - | `[{name,label,description,dimensions}]` |
| POST | /api/metrics/query | 🆕 直接查询指标（调试用） | `{metric, dimension?, start_date, end_date}` | `{rows:[...]}` |
| POST | /api/eval/compare | 🆕 触发对照评测 | `{source_a, source_b, case_ids?}` | `{run_id}` |

**🆕 `/api/tools` 响应示例**（新增 `source` 与 `risk_level`）：
```json
[
  {"name": "current_time", "source": "builtin", "risk_level": "read"},
  {"name": "query_inventory", "source": "mcp:inventory", "risk_level": "read"},
  {"name": "harness_diagnose", "source": "mcp:harness", "risk_level": "read"},
  {"name": "query_metric", "source": "builtin", "risk_level": "read"}
]
```

---

## 12. 非功能性需求

| 类别 | 要求 |
|---|---|
| 性能 | 单轮 P95 < 5s（含 RAG）；SSE 首 token < 1.5s；**🆕 MCP 外部工具单次调用 < 3s（预热后）** |
| 可用性 | docker compose up 10 分钟可演示；所有依赖有 health 检查 |
| 安全 | API key 仅存 .env（gitignore）；.env.example 脱敏；日志不打印 key；**🆕 外部 server 凭据只存键名；🆕 写操作需确认；🆕 SQL 注入用例 100% 拦截** |
| 可观测 | 100% 对话有 Langfuse trace；错误有结构化日志；**🆕 MCP 工具调用可单独辨识来源** |
| 可维护 | 三层解耦；prompt 集中管理；核心链路有测试 |
| 可扩展 | 检索策略配置化；工具注册制；LLM 通道可加；**🆕 指标口径集中在 metrics.yaml；🆕 新增 MCP server 无需改 Agent 代码** |
| **健壮性** 🆕 | **MCP 子进程崩溃可自愈；外部 server 不可用时 Agent 降级为纯 RAG 回答，不阻断主流程** |

---

## 13. 里程碑计划（37 天核心 + 5 天缓冲，每天 3-5h）

### 13.1 每日时间模型（不变）

| 每日 3-5h 分配 | 时长 | 内容 |
|---|---|---|
| ① 学原理 | 1-1.5h | 阅读讲解文档 → 我讲端到端流程 → 你复述确认 |
| ② 编码 | 1.5-2h | 增量编码（你写 + 我辅助），每步小验证 |
| ③ 验证复盘 | 0.5-1h | 跑当日验收点、排查问题、写笔记 |

**学习占比高的天**：D6 LangGraph（~60%）、D13-15 检索原理（~50%）、D20-24 评测方法论（~50%）、D25-27 MCP（~60%）、**🆕 D35 语义层设计（~40%）**。

### 13.2 里程碑

| 阶段 | 天 | 交付物 | 当日验收 |
|---|---|---|---|
| **M0 骨架+分层** | D1 | 脚手架、config、health | uvicorn 起，/health ok |
| | D2 | FastAPI 分层概念学习 + 三层解耦目录 | 三层可 import，无循环依赖 |
| | D3 | ORM 模型（学 SQLAlchemy async） | 建表成功 |
| | D4 | Redis 接入 + 日志 | redis ping 通，日志输出 |
| | D5 | Langfuse 接入（学可观测概念） | 一次 LLM 调用进面板 |
| **M1 引擎** | D6 | 学 LangGraph 状态机（重点学习日） | 能画图+复述 ReAct 流程 |
| | D7 | LLM Gateway 双通道 | CLI 直答通，Langfuse 有记录 |
| | D8 | LangGraph ReAct + current_time | 问"几点"走通工具链路 |
| | D9 | Tool Registry + 记忆滑窗 | 5 轮对话上下文正确 |
| | D10 | 摘要记忆 + sessions API ⚠️**欠账** | 21 轮摘要生效 |
| **M2 RAG 基础** | D11 | 学 RAG 全流程原理 + 文档解析切片 | 能复述 RAG 端到端链路 |
| | D12 | 向量化 + pgvector 检索 | 问文档问题有命中 |
| | D13 | 生成 + 引用定位 | 答案带 [1] 且可定位 |
| | D14 | 文档管理 API 全通 | 上传/列表/删除 |
| **M3 RAG 增强** | D15 | 学 BM25 + tsvector 原理 | 能讲清 BM25 vs 向量差异 |
| | D16 | BM25 检索实现 | 关键词命中正常 |
| | D17 | RRF 混合检索 | 优于单路（抽样） |
| | D18 | 学重排原理 + bge-reranker | 能讲清重排价值 |
| | D19 | 重排 + 检索配置化 | 3 config 可切 |
| **M4 评测** | D20 | 学评测方法论（judge/消融） | 能讲清评测设计 |
| | D21 | 评测集 50 条 + API | /api/eval/cases ok |
| | D22 | LLM-as-judge 评分器 | 单条可评分 |
| | D23 | 消融脚本 3×50 自动跑 | 3 配置全跑通 |
| | D24 | 报告生成 | 对比表+失败案例 |
| **M5 MCP+观测** | D25 | 学 MCP 协议（重点学习日）+ **🆕 注册 Harness 免费账号、建最小流水线、生成只读 PAT** | 能讲清 MCP 价值；**PAT 已入 .env** |
| | D26 | FastMCP 库存服务（**自研 server**） | 服务独立可调 |
| | D27 | MCP 客户端接入（**含 🆕 外部 Server 生命周期 + 注册 Harness + toolset 裁剪 + 只读风控**） | 对话可调库存工具；**🆕 /api/tools 可见 11 个 harness 工具** |
| | D28 | 三层埋点补全（**🆕 含 MCP span 与来源标识**） | 全链路可追踪；**🆕 能指出某次调用来自 harness** |
| **M6 UI（React）** | D29 | React 脚手架（Vite+TS+AntD）+ API client | dev server 起，/health 打通 |
| | D30 | ChatPage：消息流 + SSE 打字机 + 引用高亮 + **🆕 ToolCallCard 工具卡片** | 场景 A/B/C 浏览器可演示；**🆕 工具卡片显示来源 server** |
| | D31 | KnowledgePage + EvalPage + 会话侧栏 | 场景 D 可触发评测看报告 |
| **M7 工程化** | D32 | Docker Compose（+web 容器，**🆕 含 Node 运行时**）+ pytest + **🆕 warmup_mcp.py 预热** | 一键起，测试过 |
| | D33 | README/架构图/演示脚本（**🆕 含 MCP 接入说明与凭据配置指引**） | 5 分钟 demo 可复现 |
| | D34 | 面试三件套验收（评测报告/追踪/MCP） | 全达标（**🆕 S7 由 S10 的真实外部工具满足**） |
| **M8 前沿技术** 🆕 | **D35** | **🆕 语义层 `metrics.yaml` + `metrics_service.py` + `query_metric` 工具 + 三层防护** | **指标可查；注入用例全拦截** |
| | **D36** | **🆕 多步分析编排（prompt + analysis_trace）+ AnalyticsPage + ECharts + 评测集类型 D（10 条）** | **场景 F 出图+结论；多步题 ≥70% 通过** |
| | **D37** | **🆕 Dify 对照评测（dify_bridge + eval_compare）+ 对照报告** | **产出 eval-compare-{date}.md** |
| **M9 缓冲** | D38-42 | 缓冲（超支消化，最多 5 天） | 不推新功能 |
| | | **总周期上限 42 天（含缓冲）** | |

**红线（原有 5 条不变 + v4.0 追加 2 条）**：
1. 每天 3-5h 是总预算，学习消化时间计入，不硬赶
2. 当天验收不过 → 只修当天，不推新
3. D19 后冻结后端新功能（P1 全部砍掉，保底 P0）
4. UI 复用用户 React 经验，不安排"学前端"时间，只排"对接+打磨"
5. 缓冲天只用于补进度，不用于加需求
6. **🆕 D37 是"可选阻断点"——若进度超支，D37（Dify 对照评测）最先砍，因为它不影响其他任何模块的正确性**
7. **🆕 D35–D37 不得提前到 D28 之前**——三项技术都依赖评测体系（D20–D24）与 MCP 客户端（D27）先就位

### 13.3 进度连续性保证（v4.0 关键说明）

| 事项 | 是否受影响 |
|---|---|
| 已完成 13 天（D1–D13）的教程与记录 | ✅ **不受影响**（编号未动） |
| D10 欠账的记录与补账计划 | ✅ **不变**（D10 仍是 D10） |
| 复习排期（1-3-7-14 天机制） | ✅ **不变** |
| 进度核对报告（2026-09-20） | ✅ **仍有效**，只需在 §M5 与 §M8 补注新增内容 |
| 下一步位置 | ✅ **仍是 D14 = PRD D16（BM25 检索实现）** |

---

## 14. 测试策略

| 层级 | 覆盖 | 工具 |
|---|---|---|
| 单元 | LLM Gateway 双通道/降级、RRF 融合、切片器、**🆕 语义层渲染与白名单** | pytest |
| 集成 | Agent 三路径（直答/工具/RAG）、记忆摘要、评测 API、**🆕 MCP 客户端生命周期与崩溃重启** | pytest-asyncio |
| 安全 | **🆕 SQL 注入用例（维度/指标名/时间参数）全拦截；写操作确认门；凭据不外泄（日志断言）** | pytest |
| E2E | UI 走通场景 A/B/C/D，**🆕 E/F** | 手工脚本 |
| 评测 | 评测集（**60 条**）× 3 配置；**🆕 对照评测 30 条** | eval 脚本 |

**mock 策略**：
- LLM 层用 `MockLLM`（确定性回复）测逻辑；真实模型用于最终评测
- **🆕 MCP 层用 `MockMCPServer`**（本地 FastMCP 假 server）测客户端逻辑，避免 CI 依赖外部网络与 PAT
- **🆕 Harness 侧集成测试标记为 `@pytest.mark.external`**，默认跳过，本地手动执行

---

## 15. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| LangGraph API 学习曲线 | 中 | 高 | 只用最简 ReAct 图；先 AgentExecutor 兜底 |
| bge-reranker 部署成本 | 中 | 中 | 本地 ONNX 或 API；失败降级不重排 |
| 评测集质量影响可信度 | 中 | 高 | 3 类分布固定；judge 规则写死；人工抽查 10% |
| MCP SDK 版本兼容 | 中 | 中 | 锁版本；FastMCP 最小实现 |
| 时间超支 | 高 | 高 | P1 全砍；每日红线；D19 冻结；5 天缓冲兜底 |
| API key 泄漏 | 中 | 高 | .env 不入 git；用户重置 DeepSeek key |
| **🆕 D10 欠账未补** | **高** | **高** | **数据丢失风险（Redis 单层，>8 轮/>24h/重启即丢）。补账优先级最高，建议在 D14 前插入或与 D14 合并** |
| **🆕 Harness MCP Server 迭代过快** | **高** | **中** | **锁版本号（不用 `@latest`）；规模数字引用前必读 README；PAT 失效时降级为仅自研工具** |
| **🆕 Node 子进程泄漏** | **中** | **中** | **生命周期管理 + 退出钩子 + 测试覆盖崩溃重启；`warmup_mcp.py` 统一管理** |
| **🆕 首次启动慢导致演示翻车** | **高** | **中** | **演示前必跑预热脚本；Docker 镜像预装 npm 包** |
| **🆕 多步分析失控（无限下钻）** | **中** | **中** | **step 上限 5；prompt 明确"最多下钻一层"；analysis_trace 可审计** |
| **🆕 语义层设计过复杂** | **中** | **中** | **D35 只做 3 个指标；扩展留 P1；不追求通用 BI 语义层** |
| **🆕 Harness 免费档额度变动** | **中** | **低** | **已记录 2026-08-28 一次削减；若不足以支撑演示，退回纯自研工具并保留文档说明** |

---

## 16. 面试叙事与演示脚本

### 16.1 30 秒电梯陈述（v4.0 升级）

> 我做了个企业 Agent 平台 AgentForge。它不是 demo：文档上传 → 混合检索+重排的 RAG 问答 → **通过 MCP 协议接入企业级 DevOps 平台（Harness）诊断真实流水线** → **带语义层的自主多步数据分析** → 滑窗+摘要记忆 → 全链路 Langfuse 可观测。最关键是评测体系：60 条评测集 + LLM-as-judge 打分 + 消融实验，数据证明混合+重排比纯向量准确率提升 10%+；**我还用同一套 judge 做了 Dify 与自建方案的对照评测，用数据说明为什么自建**。架构上 LLM Gateway、工具注册表、Agent 引擎完全解耦，可平滑升多 Agent。

### 16.2 5 分钟演示脚本（v4.0 升级）

1. **开场**（30s）：架构图讲分层
2. **知识库**（45s）：上传示例文档 → 状态 ready
3. **RAG 问答**（1min）：问文档问题 → 答案带引用 → 点引用看原文
4. **🆕 外部系统互操作**（1.5min）：问"我最近哪些流水线失败了，为什么" → 工具卡片显示"来源：harness" → 展开看 Agent 给出的失败分类与建议 → 切到 Langfuse 指出这次 MCP 调用的 span
5. **🆕 Agentic BI 分析**（1.5min）：问"上个月哪个仓库周转最慢，为什么" → 看 Agent 两次调用（排名 → 下钻）→ ECharts 出图 + 结论
6. **评测**（1min）：跑消融 → 展示准确率对比报告 → **🆕 顺带展示 Dify 对照报告**

> **演示时长提示**：v3.0 脚本为 5 分钟。加入 4、5 两段后约 **6.5 分钟**。若面试限时 5 分钟，可把第 6 段的对照报告改为一句话带过（"这份对照报告我放在文档里，有兴趣可以看"）。

### 16.3 面试官追问防御（v4.0 扩充）

| 追问 | 回答 |
|---|---|
| 为什么不用 Dify？ | 三段式：① 可控性（检索链路可调、可做消融）② 可评测（Dify 检索是黑盒，无法支撑 S2 的消融实验）③ **🆕 而且我实测过——我做了 30 条同题对照评测，报告在这里** |
| 检索不准怎么办？ | 展示消融实验：混合+重排提升 X%，失败案例已分析 |
| 怎么证明效果？ | 评测集 + LLM-as-judge + 失败案例分析 |
| 工具怎么扩展？ | MCP 标准协议，**不写死函数**。**🆕 实证：我加了 Harness 的 server，Agent 代码一行没改** |
| **🆕 你做过 MCP 互操作吗？** | **做过。接入了 Harness 官方 MCP Server（11 工具 × 252 资源类型），工具是运行时 `tools/list` 动态发现的；我用 toolset 裁剪控制上下文膨胀，并按风险分级把写操作限制为只读** |
| **🆕 Agent 生成的 SQL 出错了怎么办？** | **我的设计里 Agent 不写 SQL。它只能从 `metrics.yaml` 选指标名和维度，SQL 由模板渲染 + 参数绑定，维度走白名单——所以不存在"生成错 SQL"这个失败模式** |
| **🆕 多步分析和普通问答有什么区别？** | **普通问答是一次工具调用；多步是 Agent 拿到初步结果后自己决定再下钻。我的评测集类型 D 专门判"调用序列是否合理"，而不是只看最终答案** |
| 多 Agent 怎么做？ | 架构已预留：LangGraph 图嵌子图 + 工具集隔离（阶段 2 roadmap） |
| 并发性能？ | 当前单体 + Redis 缓存；阶段 3 平台化加队列/多通道 |
| **🆕 外部服务挂了怎么办？** | **MCP 子进程崩溃会退避重启；连续失败时该工具下线，Agent 降级为纯 RAG 回答，不阻断主流程** |

### 16.4 话术安全边界（v4.0 新增，重要）

**可以说的**：
- "我在做 Agent Runtime 的核心组件：ReAct 循环、工具注册、上下文与状态管理、LLM Gateway、MCP 接入、全链路追踪"
- "我实现了一个带语义层的分析工具，避免让模型直接生成 SQL"

**不要说（会被追问打穿）**：
- ❌ "我造了一个 Agent Harness" —— 被追问"权限模型、沙箱边界、预算上限、checkpoint 回滚怎么设计"时无话可说
- ❌ "我做了完整的 Agentic BI 平台" —— 被追问"多租户、审计、语义层版本管理"时无话可说
- ❌ "我的 Agent 能自己写 SQL" —— 这是风险，不是亮点

**安全说法**："安全和预算护栏是我的下一步（阶段 3）"——**主动划边界比硬撑更显专业**。

---

## 17. 附录

### 17.1 目标 JD 映射（18 份真实岗位）— v4.0 更新

| JD 要求 | 覆盖模块 | 阶段 |
|---|---|---|
| LangChain/LangGraph（100%） | F2 Agent 引擎 | 1 |
| RAG 全流程（100%） | F3 RAG 引擎 | 1 |
| Tool Use/Function Calling（100%） | F6 工具注册 | 1 |
| FastAPI/异步（100%） | 全后端 | 1 |
| 评测体系（阿里云/华为点名） | F7 评测 | 1 |
| MCP（远程岗必考） | F6.3/6.4 | 1 |
| 可观测（海外远程） | F8 Langfuse | 1 |
| 记忆管理（华为） | F5 记忆 | 1 |
| **🆕 企业级工程化（高端岗第 2 项）** | **F6.6–F6.12 外部 MCP 接入与风控** | **1** |
| **🆕 数据分析 Agent（高端岗第 8 项）** | **F11 / §9.10 Agentic BI** | **1** |
| **🆕 效果评估方法论（横向对照）** | **F7.7/F7.8 对照评测** | **1** |
| 多智能体协作（阿里云/华为） | - | 2 |
| Agent Runtime/Harness（agent 侧含义，见概念融合评估附录 A） | - | 3 |

### 17.2 示例演示文档清单 — 不变
- `data/sample_docs/公司考勤制度.md`（含年假/加班/请假规则）
- `data/sample_docs/产品手册.pdf`（产品规格）
- `data/sample_docs/FAQ-常见问题.md`

### 17.3 🆕 演示前置检查清单（v4.0 新增）

演示前必须逐项确认（**任一项不过则 demo 会翻车**）：

| # | 检查项 | 命令/方式 |
|---|---|---|
| 1 | Harness PAT 有效 | `curl -H "x-api-key: $HARNESS_API_KEY" https://app.harness.io/gateway/pipeline/api/pipelines/list` |
| 2 | MCP Server 已预热 | `python scripts/warmup_mcp.py` |
| 3 | 11 个工具已注册 | `curl localhost:8000/api/tools \| jq '.[] \| select(.source=="mcp:harness")'` |
| 4 | 示例文档已入库 ready | 知识库页确认 |
| 5 | 指标语义层可查 | `curl localhost:8000/api/metrics` |
| 6 | 证据数据库有演示数据 | 确认 `inventory_movements` 有近 30 天数据（否则场景 F 无结论） |
| 7 | 一份已生成的评测报告 + 一份对照报告在 `docs/` 下 | 避免现场跑评测等待 |

### 17.4 🆕 来源与依据（v4.0 新增）

| 内容 | 来源 | 日期 |
|---|---|---|
| Harness MCP Server 规模与配置 | `github.com/harness/mcp-server` README（直读） | 2026-09-22 |
| Dify App 可暴露为 MCP Server | 本地 Dify 1.17 源码（`api/controllers/mcp/mcp.py` 等） | 2026-09-21 |
| Agentic BI 定义与语义层前置 | thoughtspot.com/glossary/agentic-bI；衡石 2026 数智化趋势 | 2026-09-21 |
| PRD 已有接入点 `POST /api/mcp/register` | `docs/PRD.md` §11（第 615 行） | — |
| 项目进度与欠账 | `docs/进度核对报告-2026-09-20.md` | 2026-09-20 |

---

*— PRD v4.0（含前沿技术融入）完 ｜ 下一里程碑：D14 = PRD D16「BM25 检索实现」（不变） —*
