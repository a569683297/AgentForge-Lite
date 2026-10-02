# AgentForge 阶段 1 — 详细产品需求文档（PRD v4.1 · 含前沿技术融入版）

> **文档版本**：v4.1（在 v4.0 基础上修正分析数据源、补齐 Langfuse 数据回流、加固对照评测公平性）
> **编制日期**：2026-09-22
> **上级文档**：`docs/PRD.md`（v3.0，2026-08-28）—— **本文件不覆盖任何既有文档，三者并存**
> **前序版本**：`docs/PRD-v4-含前沿技术.md`（v4.0）—— 被本版取代，保留以查沿革
> **配套分析**：`docs/概念融合评估-2026-09-21.md`（可行性判定与接入步骤）｜ `docs/可分析数据资产清单-2026-09-22.md`（数据资产盘点）
> **当前进度**：账面 D13 / D34（教程 D01–D13；D10 摘要记忆为已知欠账）
> **演进关系**：阶段 1（RAG+ 增强版，**37 天核心 + 5 天缓冲，上限 42 天**）→ 阶段 2（多 Agent 版，+14 天）→ 阶段 3（完整 Runtime 平台，+14 天）

### 版本沿革

| 版本 | 日期 | 核心变化 | 核心天数 |
|---|---|---|---|
| v3.0 | 2026-08-28 | 原始 PRD，RAG+ 增强版 | 34 + 5 缓冲 |
| v4.0 | 2026-09-22 | 融入 Harness.io MCP / Agentic BI / Dify 对照评测三项前沿技术 | **37 + 5 缓冲** |
| **v4.1** | **2026-09-22** | **① Agentic BI 数据源从「虚构业务库」修正为「系统自身运行数据」② 补齐 Langfuse 数据回流（F8.5 + §9.12）③ 加固 Dify 对照评测的公平性前提 ④ 补 `eval_case_results` 表** | **37 + 5 缓冲（不变）** |

> **v4.1 的动机（一句话）**：v4.0 的 Agentic BI 把数据源设在了一个项目里并不存在的库存业务上——业务语境断裂（知识库问答项目为什么管库存？面试一问即穿）。**v4.1 把分析对象改回系统自己产生的数据**：这个项目本身就在持续生产结构化数据（评测结果、文档切片、对话记录、运行追踪），分析自己是自洽的、也是真实需求。同时补上 Langfuse 这条唯一被遗漏的取数链路。

---

## 0. 变更总览（v4.0 引入前沿技术 / v4.1 修订数据源与回流）

### 0.1 三项技术的融入方式

| 技术 | 融入方式 | 新增天数 | 含金量类型 | 接入位置 |
|---|---|---|---|---|
| **Harness.io MCP** | **并入 D26/D27**（MCP 那两天本来就在做） | **0 天** | 协议层：真实外部企业系统互操作 | `mcp_client.py` → Harness 官方 MCP Server |
| **Agentic BI** | **新增 D35–D36** | 2 天 | 能力层：自主多步分析 + 发挥前端主力 | Tool Registry + `metrics.yaml` 语义层 + ECharts |
| **Dify 对照评测** | **新增 D37** | 1 天 | 方法论：把"为什么自建"从论证表变实测数据 | Eval Service（judge 增加外部答案输入源） |
| **Langfuse 数据回流** 🆕v4.1 | **并入 D28**（可观测那天本来就在写 Langfuse） | **0 天** | 能力层：把性能/成本也变成可分析对象 | `langfuse_client.py` → 语义层第二数据源 |

**合计新增 3 天**：核心 D1–D34 → **D1–D37**；缓冲 D38–D42（5 天）；总上限 **42 天**。

### 0.2 里程碑编排的四条铁律（本版最重要的设计决策）

1. **D1–D34 的编号与内容保持不变**——已完成 13 天的进度记录、教程（D01–D13）、复习排期（1-3-7-14）与进度核对报告**全部不受影响**，无需任何回溯修正。
2. **能并入原有天的绝不新增天数**——Harness 接入并入 D26/D27（MCP 客户端本就必写），前端图表并入 D30/D31（ChatPage 本就有图表能力）。
3. **只有真正的新能力才编新天数**——Agentic BI 语义层（D35–D36）与 Dify 对照评测（D37）是 v3.0 范围外的新增能力，因此单独编号。
4. **🆕v4.1 追加：修订不新增天数**——v4.1 的 4 组改动（数据源修正、Langfuse 回流、对照公平性、明细表）**全部并入既有天数**，核心仍为 37 天、缓冲仍为 5 天。**改口径不等于加范围**，进度账不变。

> ⚠️ **口径说明**：原 v3.0 表格中 D35–D39 为缓冲天。本版将 D35–D37 改为新增工作天，缓冲顺延为 D38–D42。这不是"加需求"，而是把两项新能力显式排期，避免它们以"顺手做做"的名义挤占缓冲、最终挤掉验收。

### 0.3 未变更的部分（明确列出，避免误解）

以下章节**完全沿用 v3.0**，本版仅做必要的一致性同步：
- 技术栈与选型（§8.4）
- 分层架构红线（§8.2）
- F1–F10 功能需求（§7，仅 F6 有扩展）
- RAG 检索链路与 BM25/tsvector 的术语澄清（§9.3）
- 数据模型主体（§10，v4.0 新增 1 张表 + v4.1 新增 1 张表 = 共 2 张）
- 测试策略（§14）
- 既有 S1–S9 成功指标（§4，仅新增 S10–S15）

### 0.4 v4.1 相对 v4.0 的变更（本版全部改动，共 4 组）

#### ① 分析数据源修正（核心）★

**问题**：v4.0 的 Agentic BI 把数据源设在 `inventory_movements`（库存业务表）。但实测：`app/models/` 只有 4 张表（`sessions`/`messages`/`documents`/`document_chunks`），**全是 RAG 场景数据**；`inventory` 在 `app/` 与 `mcp_servers/` **零命中**。原 PRD 也明确写的是"内存 mock 数据"。

**结论**：业务语境断裂——企业知识库问答项目为什么管库存？面试一问即穿。

**修正**：Agentic BI 的分析对象改为**系统自身运行数据**（评测结果、文档切片质量、对话记录、MCP 健康度、运行追踪）。

**为什么这是更好的方案**：

| # | 优势 |
|---|---|
| 1 | **数据天然存在**——跑一次评测、聊几轮对话就有，不需要造任何业务数据 |
| 2 | **业务自洽**——Agent 平台的运营者当然需要知道自己系统跑得怎么样，这是真实需求 |
| 3 | **与项目最强项形成闭环**——评测产出数据 → Agent 消费 → 给出优化建议。评测体系是本项目唯一的真差异化，现在它同时是数据源 |
| 4 | **多步下钻是真需要**——趋势 → 哪个维度弱 → 哪类用例失分 → 根因在切片，这是真下钻，不是为演示硬凑两步 |
| 5 | **面试叙事升级**——从"我做了个库存 demo（编的）"变成"**我的 Agent 能分析自己的运行数据并给出优化建议**"，这恰是 AI 平台团队的真实日常工作 |

**库存路径的处理（降级但保留）**：`inventory_server`（D26）保留，但**定位从"业务场景"改为"多数据源能力演示"**——同一个 Agent 既能查非结构化文档，也能查结构化业务库，这是"企业 Agent 平台"的合理能力。**但它不再作为 S11/S12 的指标依据**（指标必须靠系统自身数据）。

#### ② 补齐 Langfuse 数据回流（新增 F8.5 + §9.12）★

**问题**：F8 只写了"埋点 + 面板展示"，**没写"数据回流给 Agent 分析"**。Langfuse 有自己的数据库，数据不在项目 PostgreSQL 里——所以"我最近延迟为什么涨"这类问题，Agent 根本答不出来。

**修正**：新增 `langfuse_client.py` 取数通道 + 语义层支持第二数据源（`source: langfuse_api`），让性能与成本数据也成为可分析对象。

#### ③ 加固 Dify 对照评测的公平性前提（§9.11）

v4.0 只写了对照流程，没写**怎么比才公平**。v4.1 补入四条前提：文档同源、Dify 侧调优、只比答案层、砍 D37 的代价说明。

#### ④ 补 `eval_case_results` 表（§10）

`eval_runs` 只存汇总分，**答不出"哪类用例失分最多"**——这是跨表下钻链的第 2 跳，缺它整条链断掉。且 PRD §9.5 报告结构本就要求"失败案例各取 3 条"，这张表是评测功能自身就该有的。

**天数影响：0。** 四组改动全部并入既有天数（①→D35/D36 口径调整，②→D28，③→D37 内，④→D22 内）。

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
>
> **🆕v4.1 补前提**：实测数据要站得住，必须满足**公平性四前提**（文档同源 / Dify 侧调优 / 只比答案层 / 明确可比范围）——见 §9.11.1。**否则"用数据说话"会变成"用不公平的数据说话"，反而更糟。**

### 2.4 新增的差异化来源（为什么加这几项）

| # | 新差异化点 | 对应 JD 要求 | 为什么大多数人做不到 |
|---|---|---|---|
| 1 | **外部系统 MCP 互操作** | MCP 协议（远程岗必考）；企业级工程化（高端岗第 2 项） | 多数人只写过自研 MCP server，没接过真实企业平台 |
| 2 | **自主多步分析**（🆕v4.1 改为分析系统自身数据） | 数据分析 Agent（高端岗第 8 项） | 多数人只做文档问答，不敢碰结构化数据（怕 SQL 出错）；更少人会想到**分析自己系统的运行数据** |
| 3 | **语义层设计** | 架构设计能力 | "让 LLM 不写 SQL"是反直觉决策，讲出来就是设计品味 |
| 4 | **平台对照评测方法论** | 效果评估体系（阿里云点名） | 多数人只评自己，没做过"自建 vs 平台"的横向对照 |
| **5** | **🆕v4.1 可观测数据回流** | 可观测性 / 平台工程 | 多数人接了 Langfuse 就停在"看板能看"，**不会想到把追踪数据做成 Agent 可查询的分析对象** |

> **🆕v4.1 对第 2 项的强化说明**：v4.0 说"敢碰结构化数据"就够差异化；v4.1 把数据源改到**系统自身运行数据**后，差异化的质地变了——从"我敢做数据分析"变成"**我设计了一个能自我诊断的系统**"。后者的叙事强得多，而且不需要编造业务场景。

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

### 4.2 v4.0 / v4.1 新增指标（S10–S15）

| # | 指标 | 目标值 | 验证方式 | 优先级 |
|---|---|---|---|---|
| S10 | **外部 MCP 互操作** | 接入 ≥ 1 个**非自研** MCP Server（Harness），工具可被发现并成功调用 | 演示脚本 + Langfuse 追踪 | 必须 |
| S11 | **语义层覆盖** | `metrics.yaml` 含 ≥ 3 个**系统自身**指标定义（v4.1 口径），含 ≥ 1 个支持多步下钻的明细指标 | 配置文件 + 演示 | 必须 |
| S12 | **多步分析正确率** | 评测集类型 D（多步分析）≥ 70% 通过 | 评测报告 | 应该 |
| S13 | **工具写操作防护** | 100% 写操作需显式确认；SQL 注入测试用例全拦截 | 安全测试脚本 | 必须 |
| S14 | **平台对照报告** | 产出 Dify vs AgentForge 同题对照报告（**30 条**，v4.1 统一口径） | Markdown 报告 | 应该 |
| **S15** 🆕v4.1 | **Langfuse 数据可分析** | 语义层含 ≥ 1 个来自 Langfuse 的指标（延迟 P95 或 token 成本），Agent 可自然语言查询并出图 | 演示 + 指标定义文件 | 应该 |

> **S7 的口径澄清（重要）**：v3.0 中 S7 的"真实外部工具"由自研的 `inventory_server`（内存 mock 数据）承担，**严格讲不满足"真实外部"**。v4.0 后 S7 由 **S10 的 Harness 外部 Server** 真实满足，而 `inventory_server` 转为"自研 server（理解协议两端）"的教学与测试用途。**两者都需要保留**——自己写 server 证明懂协议，接别人的 server 证明能互操作。

> **S11 的口径澄清（v4.1 新增）**：v4.0 中 S11 的指标示例是库存业务指标（`inventory_turnover` 等），与项目底座（RAG 知识库）业务语境不符。v4.1 改为**系统自身运行指标**——这既解决了语境问题，也让"数据天然存在、无需造数"。

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

**场景 F（v4.0 新增 / v4.1 修正数据源）★：Agentic BI 自主多步分析——分析系统自身**

> 用户问："最近三次评测，准确率趋势怎么样？哪个维度最弱、为什么？"
> → Agent 判断这是指标类问题 → 调用 `query_metric(metric="eval_accuracy", dimension="config_name", ...)`
> → 工具从 `metrics.yaml` 读取指标口径，**用模板渲染 SQL**（LLM 全程不写 SQL）→ 返回 `pure_vector 79% / hybrid 84% / hybrid_rerank 88%`
> → Agent **自行判断**"hybrid_rerank 最优"，并**自主决定下钻**：`query_metric(metric="eval_failures_by_category", ...)`
> → 发现 `cross_doc`（跨文档推理）类失分最多
> → Agent **继续下钻第三轮**：`query_metric(metric="chunk_quality", dimension="document_id", ...)`
> → 定位到这 5 条题引用的文档**超短切片（< 50 字）占比 62%**
> → 前端 ECharts 渲染 + 结论："问题在分块策略，不在检索算法，建议调整 chunk_size 后重跑评测"
>
> **含金量**：三步之间**没有人类介入**，是 Agent 自己决定要往下查——这就是"自主多步"，也是 Agentic BI 与上一代 ChatBI 的分界线。
>
> ⚠️ **v4.1 修正说明**：v4.0 此场景用的是库存业务数据（`inventory_turnover` 等），与项目底座（RAG 知识库）业务语境不符。已改为**系统自身评测数据**——数据天然存在、业务自洽，且与项目最强项（评测体系）形成闭环。

**场景 G（v4.0 新增 / v4.1 加固前提）：平台对照评测**

> 管理员用同一批 **30 条**问题，分别投给（a）本地 Dify 应用、（b）AgentForge → 两者答案交给**同一个 judge**（同 prompt、同口径、temp=0）打分 → 生成对照报告：各自准确率、各自失败模式、结论 → 报告成为"为什么自建"的**实测证据**
>
> ⚠️ **v4.1 公平性前提（四条，缺一不可，详见 §9.11）**：① **文档同源** ② **Dify 侧须调优后参赛** ③ **只比答案层，不比检索层** ④ 明确砍掉 D37 的代价

**场景 H（v4.1 新增）★：系统性能数据的自然语言分析（Langfuse 回流）**

> 用户问："最近一周响应延迟的趋势怎么样？哪一段最慢？"
> → Agent 识别这是运行指标问题 → 调用 `query_metric(metric="latency_p95", dimension="span_name", ...)`
> → 工具**不查 PostgreSQL**，而是走 `langfuse_client` 查询 **Langfuse API**（语义层支持第二数据源）
> → 返回分阶段 P95：embedding 0.3s ／ 向量检索 0.5s ／ BM25 0.4s ／ **重排 1.8s** ／ 生成 2.1s
> → Agent 判断"重排 + 生成占大头"，自主下钻 `query_metric(metric="token_cost", dimension="model")`
> → 折线图 + 结论："P95 4.1s（预算 5s 内）；重排占 44%，若需提速可把候选集从 top20 降到 top10"
>
> **含金量**：这条链把**性能与成本**也纳入 Agent 的可分析范围——这类数据在 v4.0 里是"只在 Langfuse 面板能看、Agent 完全拿不到"的孤岛。

---

## 6. 范围

### 6.1 In Scope（阶段 1 必须交付）

**原有 10 项（不变）**
1. LLM Gateway（DeepSeek + OpenAI 双通道，统一接口，Langfuse 埋点）
2. Agent 引擎（LangGraph ReAct，5 步上限，工具/检索决策）
3. RAG 引擎（解析 → 切片 → 向量化 → 混合检索 → 重排 → 引用生成）
4. 工具注册中心 + MCP 接入（自研 MCP 库存服务 demo，官方 SDK `MCPServer`）
5. 记忆系统（滑窗 8 轮 + 20 轮摘要 + Redis/PG 持久化）
6. 评测体系（**基础 50 条**评测集 + LLM-as-judge + 消融实验 + 报告；D36 追加类型 D 10 条后共 60 条）
7. Langfuse 可观测（LLM/检索/工具三层埋点）
8. Web UI（对话页 + 知识库管理 + 引用高亮 + 评测报告页）
9. 部署（Docker Compose：pgvector + redis + langfuse + api + web）
10. 测试（pytest 核心链路）+ 文档（README/架构图/演示脚本）

**v4.0 新增 4 项**
11. **外部 MCP Server 接入**：接入 Harness 官方 MCP Server（`harness-mcp-v2`），工具动态发现 + 注册 + 调用 + 生命周期管理
12. **MCP Server 管理**：`mcp_servers` 表持久化 + 重启自动重连 + toolset 裁剪 + 写操作风险分级
13. **自主多步分析能力**：`metrics.yaml` 指标语义层 + `query_metric` 工具 + 多步下钻 + 前端图表（**🆕v4.1 分析对象为系统自身运行数据**）
14. **平台对照评测**：Dify 应用作为外部答案源接入 judge，产出对照报告（**🆕v4.1 含公平性四前提**）

**🆕v4.1 新增 2 项**
15. **Langfuse 数据回流**：只读取数通道 `langfuse_client.py` + 语义层双数据源（`sql` / `langfuse_api`），使延迟与 token 成本可被 Agent 分析
16. **评测明细落库**：`eval_case_results` 表，支撑"分类失分归因"与报告的失败案例抽样

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
| **🆕v4.1 分析外部业务数据（如虚构的库存业务场景）** | **明确不做**（业务语境与 RAG 知识库底座断裂，面试一问即穿。分析对象锁定为系统自身运行数据） |
| **🆕v4.1 对照 Dify 的检索层指标（召回率/MRR/NDCG）** | **明确不做**（Dify 不暴露 chunk 级检索结果，拿不到它的召回集。对照只到答案层，见 §9.11.3） |
| **🆕v4.1 把 Langfuse 追踪数据双写落库** | **明确不做**（红线 7：只读回流，避免双写不一致） |

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

> **实施修订（2026-09-23，D14）——answer 节点已合并进 plan**：
> F2.1 / F2.4 写的"无工具调用时直接走 answer"在实测中有一处浪费：plan 的 LLM 在不需要工具时
> **已经把答案生成完了**，条件边再走 answer 又调一次 LLM 重新生成 —— 一轮白烧一次调用，
> PG 里留下两条内容相近的 assistant（实测 `messages` 表每轮都成对出现）。
> 现改为：**plan 同时承担"决策"与"回答"**，无 `tool_calls` 时直接 END，answer 节点移除。
> LLM 调用数降到理论最少（无工具轮 1 次／有工具轮 2 次）；D11 的引用规范（`[n]` 标注）
> 已整体并入 plan 的 system prompt，未退化。
> 验收证据：`scripts/d14_sessions_verify.py` C 段 —— 两种路径下"无 tool_calls 的 assistant"均恰好 1 条。
> 另两点如实记录：实现里**没有独立的 `observe` 节点**，工具结果由 `execute` 直接写成 `tool` 消息；
> `recursion_limit` 已显式设为 **12**（原先只在注释写"10"、代码从未传给 LangGraph，实际生效的是库默认值 25）。

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
- **F6.4** MCP demo 服务：`mcp_servers/inventory_server.py`（官方 SDK 的 `MCPServer`，内存 mock 数据），暴露 `query_inventory(filters)`
- **F6.5** 工具调用失败不影响主流程
- **验收（原）**：对话中问"查库存数量小于 10 的商品"→ 走通 MCP 工具 → 返回 mock 数据

**v4.0 新增条目**

- **F6.6 外部 Server 注册**：`POST /api/mcp/register` 支持注册**任意标准 MCP Server**（不限于自研），入参扩展为 `{name, command, args, env, toolsets?}`。首个目标：Harness 官方 Server（`npx -y harness-mcp-v2@latest`）
- **F6.7 生命周期管理**：MCP Client 必须管理子进程全生命周期——启动、`initialize` 握手、健康检查、**崩溃自动重启（含退避）**、应用退出时清理。**防止 Node 子进程泄漏**
- **F6.8 Server 持久化**：`mcp_servers` 表记录已注册 server 的配置与状态，应用重启后**自动重连**，无需重复注册
- **F6.9 工具动态发现**：工具清单**运行时**从 server 的 `tools/list` 获取并注册（**不写死在代码中**）。新增 server 不需要改 Agent 代码
- **F6.10 toolset 裁剪**：支持只启用部分 toolset（Harness 实测 **42** 个）。理由：**工具数量膨胀会降低 LLM 选工具的准确率**
- **F6.11 写操作风险分级**：工具按风险分级（`read` / `low_write` / `medium_write` / `high_write`），写操作需**显式确认**才执行。参考 Harness 的 `HARNESS_AUTO_APPROVE_RISK` 设计，阶段 1 默认**只读**
- **F6.12 凭据安全**：外部 server 的密钥（如 `HARNESS_API_KEY`）**只从 `.env` 读取**，不写入数据库、不写入代码、不打印日志
- **验收（新增）**：
  - 启动后 `/api/tools` 能列出 Harness 的 11 个工具（名称 + 描述 + 参数 schema）
  - 问"我最近哪些流水线失败了"→ Agent 选中 `harness_list` / `harness_diagnose` → 返回真实数据 → Langfuse 可见该 span
  - 手动 kill 子进程 → 客户端自动重启并恢复可用
  - 用 PAT 尝试**写操作** → 被服务端 `HARNESS_READ_ONLY=true` **拦截**并给出明确提示（**⚠ 修正 ④：不是靠「只读 PAT」**，个人 PAT 没有只读档，见 §9.6.2 汇总）

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
- **🆕 F7.10 评测明细落库（v4.1 新增）** ★：评测过程**必须逐条写入 `eval_case_results`**（`run_id` + `case_id` + 三维度得分 + `sequence_ok` + 是否通过）。
  - **为什么这是评测功能自身就该有的**：PRD §9.5 的报告结构本来就要求"**失败案例各取 3 条**"——没有单用例明细就取不出失败案例。v4.0 漏了这张表。
  - **为什么它对分析能力是必需的**：`eval_runs` 只有汇总分，**汇总分不可再下钻**。缺这张表，"哪类用例失分最多""哪个维度最弱的原因"**全都答不出来**——这是场景 F 下钻链的第 2 跳，缺它整条链断在第二跳。
  - **验收**：跑完一次评测后，`SELECT category, AVG(score_faithfulness) FROM eval_case_results r JOIN eval_cases c ON r.case_id=c.id WHERE run_id=? GROUP BY category` 能直接返回分类得分

### F8 Langfuse 可观测（P0）— **v4.1 扩展**

**原有条目（不变）**
- **F8.1** 部署：Langfuse Cloud（免费额度）或 Docker 自托管
- **F8.2** 埋点：LLM 调用、检索、工具（入参出参）、Agent 轨迹（trace/span）
- **F8.3** UI 展示：会话追踪树、token 消耗、延迟分析
- **验收（原）**：一次"带工具调用的对话"在面板中完整可见全链路

> **v4.0 补充（F8.4）**：**MCP 工具调用必须单独可辨识**——span 元数据需含 `mcp_server_name` 与 `tool_name`，以便演示时能明确指出"这一次是 Harness 外部 server 的调用，不是自研工具"。

**🆕 v4.1 新增条目 —— 数据回流（本节为 v4.1 的核心改动之一）**

- **F8.5 Langfuse 数据回流** ★：新增 `app/services/langfuse_client.py`，**通过 Langfuse REST API 反向读取追踪数据**，使性能与成本数据成为 Agent 可分析的对象。
  - **F8.5.1 取数通道**：封装 `/api/public/traces` 与 `/api/public/observations`，支持按时间范围、名称、标签过滤与分页；凭据（`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`）**只从 `.env` 读**（遵守 §8.2 红线 4）。
  - **F8.5.2 只读原则**：Langfuse 是**外部数据源**，项目**不把它双写落库**——避免双写不一致。采用实时查询 + 可选短期缓存（如 60s）。
  - **F8.5.3 语义层集成**：`metrics.yaml` 的指标支持 `source: sql | langfuse_api` 两种数据源（详见 §9.10.5）。
  - **F8.5.4 降级兜底**：Langfuse 不可达时，仅该指标返回明确错误，**不阻断主流程**（SQL 类指标仍可用），Agent 应能向用户说明"该指标暂不可用"。
  - **验收（新增）**：
    - 自然语言问"最近一周延迟趋势怎么样、哪一段最慢" → 两次调用（分阶段延迟 → token 成本）→ **出折线图 + 结论**
    - `GET /api/metrics` 返回中能区分 `source: sql` 与 `source: langfuse_api`
    - Langfuse 停掉后，SQL 类指标仍正常工作

> **为什么这条必须补**：v3.0/v4.0 的 F8 只写了"**埋点 + 面板展示**"——数据进了 Langfuse，但**没有任何回流路径给 Agent**。而 Langfuse 有自己的数据库，数据不在项目 PostgreSQL 里。结果是延迟、token 成本、各段耗时这些**最有运维价值的数据，Agent 完全拿不到**。补上 F8.5 后，"分析系统自身运行数据"才是完整的：**结构化数据走 SQL，运行追踪走 Langfuse API。**

### F9 Web UI（P0，React + TS + Vite）— **v4.0 扩展**

**原有条目（不变）**
- **F9.1** 对话页（ChatPage）：消息流、SSE 流式展示（打字机效果）、引用高亮（Citation 组件点击展开原文）、新建会话
- **F9.2** 知识库页（KnowledgePage）：上传、进度状态、文档列表、删除
- **F9.3** 评测页（EvalPage）：触发评测、轮询进度、渲染 Markdown 报告
- **F9.4** 侧边栏：模型选择（deepseek/openai）、会话历史
- **F9.5** 技术要点：axios + fetch 处理 SSE；vite dev proxy → :8000；Ant Design 组件库
- **验收（原）**：浏览器完整走通场景 A/B/C/D；打字机流式 + 引用高亮可用

**v4.0 新增条目 / v4.1 修正验收**

- **F9.6 工具调用可视化**：对话中工具调用需以**独立卡片**呈现（工具名 / 来源 server / 入参 / 返回摘要 / 耗时），可折叠展开。目的：让"Agent 调了外部系统"这件事**在 UI 上可见**，而不只在 Langfuse 里可见
- **F9.7 分析页（AnalyticsPage）**：自然语言提问 → Agent 多步分析 → **ECharts 图表渲染**（柱状/折线/趋势）。关键：`query_metric` 返回**结构化数据（dict）而非字符串**，前端直接消费
  - **🆕v4.1**：图表数据来源为**系统自身指标**（评测/切片/延迟），不再指向库存业务数据；**同一页面既能渲染 SQL 源指标，也能渲染 Langfuse 源指标**（对前端无差别）
- **F9.8 MCP Server 管理面板**：列出已注册 server、连接状态、工具数量、启用/禁用（P1 可降级为只读展示）
- **验收（新增 / v4.1 修正）**：浏览器内问"**最近三次评测准确率趋势怎么样、哪个维度最弱**" → 出图 + 结论；**🆕v4.1** 问"最近延迟趋势"也能出图（走 Langfuse 源）；对话内工具卡片能显示"来源：Harness"

### F10 部署与测试（P0）— 不变
- **F10.1** docker-compose.yml：pgvector + redis + langfuse + api + web
- **F10.2** pytest：核心链路（LLM 直答/工具/RAG 检索/记忆/评测 API）
- **F10.3** README（架构图 + 启动指南 + 演示脚本）
- **验收**：全新环境 docker compose up 后 10 分钟可演示

> **v4.0 补充（F10.4）**：**Node 运行时依赖**——Harness MCP Server 是基于 Node 的子进程，Docker 镜像需包含 Node（或文档中明确宿主要求）。同时**首次启动预热**必须写入启动脚本（`npx` 拉包 + 可选下载约 23MB ONNX 模型），否则演示首问会卡顿。

### F11 Agentic BI 分析能力（P0，v4.0 新增）★

> 完整设计见 §9.10。**核心技术决策：LLM 不写 SQL，只选指标名 / 维度 / 时间范围，SQL 由语义层模板渲染。**
>
> **🆕 v4.1 口径修正（重要）**：分析对象从"**虚构的库存业务数据**"改为"**系统自身运行数据**"。理由见 §0.4 ①。**指标来源必须是项目真实产生的数据**，不得为演示而构造业务表。

- **F11.1 指标语义层**：`metrics.yaml` 定义 ≥ 3 个指标（含 ≥ 1 个用于下钻的明细指标），每项含 `label / description / source / table / time_column / dimensions（白名单）/ sql_template`
  - **🆕 v4.1**：新增 `source` 字段，取值为 `sql`（项目 PostgreSQL）或 `langfuse_api`（Langfuse 回流，见 F8.5）
  - **🆕 v4.1**：首批指标共 **6 个**，全部来自系统自身数据（清单见 §9.10.3）：`eval_accuracy`（准确率趋势）/ `eval_dimension_scores`（三维度得分）/ `eval_failures_by_category`（★ 明细下钻）/ `doc_processing_success`（文档处理成功率）/ `chunk_quality`（★ 明细下钻，切片质量）/ `latency_p95`（★ Langfuse 数据源）
- **F11.2 语义层加载与渲染**：`metrics_service.py` 负责加载 YAML、校验指标与维度白名单、按模板渲染 SQL、参数化绑定时间范围；**按 `source` 分派到 SQL 执行器或 Langfuse 客户端**
- **F11.3 查询工具**：`query_metric(metric, dimension?, start_date, end_date)` 注册进 ToolRegistry，**返回结构化 dict 而非字符串**（供前端图表直接消费）
- **F11.4 三层防护**：① 指标名白名单 ② 维度白名单（`{dimension}` 只能取 `dimensions` 中的值）③ 时间走参数绑定。三者缺一不可
- **F11.5 多步分析编排**：Agent 允许在拿到初步结果后自主发起第二轮下钻调用；`analysis_trace` 记录完整调用序列（供 F7.6 判定 `sequence_ok`）
- **F11.6 分析页**：`AnalyticsPage` + `MetricChart`（ECharts），渲染柱状/折线图，并展示 Agent 结论与数据出处
- **🆕 F11.7 双数据源分派**：`source: sql` 走 `db.fetch_all`；`source: langfuse_api` 走 `langfuse_client`。两条路径对上层（Agent / 前端）**接口完全一致**，均返回结构化 dict
- **🆕 F11.8 语义层与数据资产一致性**：`metrics.yaml` 中每个指标必须能在 `docs/可分析数据资产清单-2026-09-22.md` 中找到对应的数据来源说明
- **验收**：
  - `GET /api/metrics` 能列出全部指标定义，且区分 `source`
  - 问"最近三次评测准确率趋势怎么样、哪个维度最弱、为什么"→ **三次工具调用**（趋势 → 分类失分 → 切片质量）→ 出图 + 结论（场景 F）
  - 问"最近一周延迟趋势、哪一段最慢"→ 走 **Langfuse API** → 出图 + 结论（场景 H）
  - SQL 注入用例（维度注入 / 指标名注入 / 时间参数注入）**100% 被拦截**
  - 不存在的指标或维度 → 返回明确错误，不落到数据库

> **v4.1 依赖提示**：场景 F 的第②跳依赖 `eval_case_results` 表（见 §10），场景 H 依赖 `langfuse_client`（见 F8.5 / §9.12）。两者都已排入本版。

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
    tsvector)   缓存)   ↕ 回流 🆕v4.1  （Python）      （Node 子进程）
        ▲               Langfuse Client              → Harness 云端 API
        │               （只读取数通道）               Dify App 🆕（可选，P1-6）
        └───────────────┘
   metrics.yaml 语义层双数据源分派（sql ｜ langfuse_api，§9.10.5）
```

**图例**：🆕 = v4.0 新增 ｜ 🆕v4.1 = v4.1 新增

> **🆕v4.1 架构变更点**：Langfuse 从"**只写不读**"变为"**写 + 回流读**"。埋点路径不变（应用 → Langfuse）；新增回流路径（Langfuse API → `langfuse_client` → 语义层 → Agent）。**这是 v4.1 唯一的架构级改动，其余都是口径与前提修正。**

### 8.2 分层架构与扩展点（阶段 2/3 零返工前提）— v4.1 更新

| 层 | 职责 | 阶段 1 实现 | 阶段 2/3 复用 |
|---|---|---|---|
| **LLM Gateway** | 所有模型调用唯一入口 | DeepSeek/OpenAI 双通道 + 埋点 | 阶段 3 加多 provider/降级/计费 |
| **Tool Registry** | 工具注册制管理 | 内置 2 + 自研 MCP 1 + **外部 MCP 11** + **分析工具** | 阶段 2 多 Agent 挂不同工具集 |
| **MCP Client** 🆕v4.0 | 外部能力接入层 | 动态发现 + 生命周期 + 持久化 | 阶段 3 多通道 Gateway 的基础 |
| **Agent 引擎** | LangGraph 图执行 | 单 Agent ReAct（多步下钻） | 阶段 2 嵌为子图 |
| **RAG Engine** | 检索链路 | 混合+重排+配置化 | 不变，多 Agent 共用 |
| **Metrics 语义层** 🆕v4.0 | 指标口径唯一来源 | `metrics.yaml` + 模板渲染 + **双数据源分派** | 阶段 2 多 Agent 共享同一口径 |
| **Eval Service** | 评测 | 评测集+judge+消融+对照+**明细落库** | 阶段 2 评测多 Agent 轨迹 |
| **可观测** | 追踪 | Langfuse 四层埋点（含 MCP） | 阶段 3 管理后台消费数据 |
| **Langfuse Client** 🆕v4.1 | 运行数据回流层 | 只读 API 取数（延迟/token/各段耗时） | 阶段 3 平台化运维面板的数据源 |

**红线（v4.0 追加 3 条，v4.1 追加 1 条）**：
1. 业务代码不得绕过 Gateway 直连 SDK
2. Agent 不得直接调函数（必须经 Registry）
3. 评测模块与业务解耦（输入配置+评测集，输出报告）
4. **🆕v4.0 任何外部凭据不得进入代码/数据库/日志，只从 `.env` 读**
5. **🆕v4.0 MCP 工具不得写死在代码中，必须运行时动态发现**
6. **🆕v4.0 结构化数据查询不得由 LLM 直接生成 SQL，必须经语义层模板渲染**
7. **🆕v4.1 Langfuse 只作只读数据源，不双写落库**——运行追踪的权威存储是 Langfuse 自身，项目库只保存业务数据；双重写入必然导致不一致，且会让"追踪数据"与"业务数据"的生命周期纠缠

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
│   │   ├── metrics_service.py 🆕 # ★ 语义层加载 + SQL 模板渲染 + 白名单校验
│   │   └── langfuse_client.py 🆕v4.1 # ★ Langfuse 只读取数通道（延迟/token 回流）
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
│   └── inventory_server.py     # 自研 MCP 库存服务（官方 SDK MCPServer）
├── metrics.yaml            🆕  # ★ 指标语义层（口径唯一来源）
├── tests/
│   ├── test_chat.py
│   ├── test_agent.py
│   ├── test_rag.py
│   ├── test_memory.py
│   ├── test_eval.py
│   ├── test_mcp_client.py  🆕  # 生命周期/重启/动态发现
│   ├── test_analytics.py   🆕  # 语义层/白名单/SQL 注入拦截
│   ├── test_langfuse_client.py 🆕v4.1 # 取数通道/降级/维度白名单
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
| MCP | 官方 `mcp` SDK（服务端入口 `MCPServer`，**原 FastMCP**；客户端 `Client`） | 标准协议，JD 必考 |
| **MCP Server（外部）** 🆕 | **Harness 官方 `harness-mcp-v2`** | **真实企业系统；官方开源；MIT；11 工具 × 259 资源类型**（2026-10-01 实测，原写 252） |
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

### 9.2 Agent 引擎（LangGraph）— v4.0 补充多步支持 / **v4.1 调整递归上限**

```
class AgentState(TypedDict):
    messages: list[dict]        # 完整对话（含工具消息）
    step_count: int
    tool_results: list[dict]
    analysis_trace: list[dict]  # 🆕 多步分析轨迹（供评测判定调用序列）
```

图结构不变，但 `should_answer` 的判定条件放宽：**当 agent 处于"分析型任务"且已获得部分结论时，允许主动再发起一轮工具调用**（场景 F 的下钻）。

> ⚠ **2026-09-23（D14）已改动此处**：`answer` 节点合并进 `plan`（原因与证据见 F2 段的"实施修订"）。
> 现在的图是 `START → plan →（有 tool_calls）execute → plan → … → END`，节点只有 plan 与 execute 两个。

- `recursion_limit` = **12**（plan 最多跑 **5** 次）
  - **🆕v4.1 为什么上调**：场景 F 的归因链是 **4 跳下钻**，共 9 个节点（plan/observe ×4 + answer）。v4.0 的 `recursion_limit = 10` 只剩 1 个余量，Agent 多想查一次就会抛 `GraphRecursionError`。**上调到 12 给出 3 个余量**，同时把"最多下钻 4 轮"写进 prompt 约束（不能只靠 limit 兜底）。
- **🆕 多步分析引导**：`prompts/analytics.py` 中明确指示——"若初步结果指向某个异常对象（如**失分最高的题目类别**），应主动下钻一层以给出原因，而非止步于排名；**最多下钻 4 轮，之后必须给出结论**"
- **🆕v4.1 超限处理**：达到下钻上限仍无结论时，Agent 必须**输出当前已获得的发现 + 说明"还需哪一步才能定位根因"**，而不是空转或编造结论

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

### 9.5 评测体系 — v4.0 扩展 / **v4.1 补明细落库**

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
> **注**：`expected_tool` 示例为自研 MCP 库存工具（D26），属评测集类型 C（工具调用）的正常用法——**库存工具本身保留**，只是不再作为分析类指标的数据源。

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

**🆕v4.1 明细落库（F7.10，重要）**：评测过程必须**逐条写入 `eval_case_results`**（`run_id` + `case_id` + 三维度得分 + `sequence_ok` + `passed` + `failure_reason`）。

- **它是上面"报告结构"的前置**：报告要求"每类明细"、"失败案例各取 3 条"——**没有单用例明细，这两项都产不出来**。
- **它是分析能力的第 2 跳**：`eval_runs` 只有汇总分，**汇总分不可再下钻**。缺这张表，场景 F 的"哪类题失分最多"答不出，整条下钻链断在第二跳。
- 所以它**不是"为 BI 加的表"，而是评测功能自身就该有的表**——v4.0 漏了，v4.1 补上。

**消融实验与分析能力的分工（v4.1 澄清）**：消融实验回答"**哪个检索配置更好**"（属评测）；多步分析回答"**为什么还不够好、根因在哪**"（属分析能力）。两者串起来才是完整的"效果可量化"闭环。

### 9.6 MCP 接入 — v4.0 大幅扩展

> **⚠ v4.1 修订说明（2026-10-01，依据 D25 实测）**：本节原按 2026-09-22 的官方文档写成，
> D25 实测后发现**四处**已不成立，已在原地逐条修正（`FastMCP` 改名 / 协议两代 / Harness 规模数字 / 「只读 PAT」口径）。
> 凡修正处均带 **「v4.1 修正」** 标记，并保留原说法以便对照 —— **不要只看结论，要看它为什么变**。

#### 9.6.1 自研 Server（保留，理解协议两端）

> **⚠ v4.1 修正 ①**：原写 `from fastmcp import FastMCP`，在 **`mcp` 2.2.0** 下**已失效** ——
> 官方把 `FastMCP` 改名为 **`MCPServer`**，旧 import 路径直接 `ImportError`
> （实测报错原文含 *"FastMCP was renamed to MCPServer"*）。
> 而 `FastMCP` 这个名字现在是 **PrefectHQ 维护的独立第三方框架**（`uv add fastmcp`），
> 与官方 SDK **同源分家** → **本项目不使用它**（选了与「官方 SDK」口径一致的官方入口）。

**mcp_servers/inventory_server.py**（官方 SDK 的 `MCPServer`）：
```python
from mcp.server import MCPServer          # ← mcp 2.2.0 的正确入口（旧路径 mcp.server.fastmcp 已失效）

mcp = MCPServer("inventory")              # name 是第一个位置参数（实测签名确认）

@mcp.tool()                               # 装饰器名未变，仍是 .tool()
def query_inventory(min_stock: int | None = None, category: str | None = None) -> list[dict]:
    """查询库存商品。min_stock: 库存下限过滤; category: 品类过滤"""
    # mock 数据：返回商品列表

if __name__ == "__main__":
    mcp.run(transport="stdio")            # run() 默认就是 stdio，这里显式写出以免歧义
```

**四项实测确认**（2026-10-01，`mcp` 2.2.0 / Python 3.12）：

| 项 | 实测结果 |
|---|---|
| `MCPServer("inventory")` | ✅ 可构造，`name` 是第一个位置参数 |
| `@mcp.tool()` | ✅ 可注册；**`inputSchema` 由函数签名自动生成**（可选参数被表达成 `anyOf: [integer, null]`） |
| `mcp.run()` 的默认 transport | ✅ **`stdio`**（签名即 `transport="stdio"`） |
| `from fastmcp import FastMCP` | ❌ `ImportError: No module named 'fastmcp'` |

- 运行：`python mcp_servers/inventory_server.py`（stdio）
- 客户端：`mcp_client.py` 连接 → 动态发现工具 → 注册进 ToolRegistry（客户端入口是 `from mcp import Client`，D27 落地）

#### 9.6.2 外部 Server 接入（v4.0 新增）★

**目标**：接入 Harness 官方 MCP Server。

**核心事实**（来源：`github.com/harness/mcp-server` README 2026-09-22 读取；**数字部分已于 2026-10-01 D25 实测覆盖**）：

| 项 | 内容 |
|---|---|
| 包 / 许可 | npm `harness-mcp-v2`（MIT），实测版本 **3.2.31** |
| 规模 | **11 个工具**（`tools/list` 实测）· **259 种资源类型** · **42 个 toolsets**（server 启动日志 + `harness_describe({})` **双重确证**） |
| **⚠ 口径警告（v4.1 新增）** | 「这个 server 有多少资源类型」**本身是欠定的** —— **各动词支持数不同**：`list` **198** / `get` **174** / `create` **92** / `update` **77** / `delete` **84** / `execute` **66** / `diagnose` **6**。**报数必须带动词口径**，否则数字之间无法比较 |
| **⚠ 已过时数字（v4.1 新增）** | 官方**文档页**现写「11 个合并工具 / 139 资源类型 / 30 toolsets」→ **已过时**；PRD 原写的 **252 / 41** 更旧。三者互不相同，**以实测（11 / 259 / 42）为准** |
| **⚠ 未实测项** | **prompt 模板数量**（原文写 35）本次**未验证** → D26/D27 若要用到再测，**在那之前不许引用这个数** |
| 工具 | `harness_list / get / create / update / delete / execute / search / diagnose / status / describe / schema` |
| 起法 | `HARNESS_API_KEY=pat.xxx npx -y harness-mcp-v2@latest`（stdio 默认）。⚠️ **国内环境必须加镜像**：`npm_config_registry=https://registry.npmmirror.com` —— 实测直连 `registry.npmjs.org` **超时**（curl 12s 返回 000），加镜像后 **2 分 31 秒**拉完；不加会**挂住 5 分钟以上且无任何输出** |
| 认证 | PAT，格式 `pat.<accountId>.<tokenId>.<secret>`（account id **自动从 token 提取**，不需另配 `HARNESS_ACCOUNT_ID`）。⚠️ **第二段是 API 层的 accountId（如 `BCGcUpagTjKhYuqnJWe83Q` 22 字符），不是界面上那个数字账号名** —— 两者不同，实测用错会得到 403 `account identifier mismatch` |
| 写操作护栏 | `HARNESS_AUTO_APPROVE_RISK` = none / low_write / medium_write / high_write / all |
| **🆕 v4.1 全局只读开关** | **`HARNESS_READ_ONLY=true`** —— 服务端**屏蔽一切写操作**（create / update / delete / execute），只放行 list / get。官方原文：*"Block all mutating operations (create, update, delete, execute). Only list and get operations are allowed."* **这是本项目实现「只读」的正解**（见下方修正 ④） |
| 诊断能力 | `harness_diagnose` 附 6 类失败分类：infra_flake / test_failure / config_error / dependency_failure / permission_error / timeout。⚠️ **实测补充**：该分类能力依赖 **`TYPESAFE_API_KEY`**，**未配置时静默跳过**（不报错、只不返回分类）|

> ⚠️ **规模数字会变**：该仓库 commit 频率接近每日，引用前须以 README 当日口径为准。
> **本项目已改为以「实测」为准**，并在 `docs/tutorials/README.md` 的「Harness 接入实测」段留存取证记录。

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
2. stdio 发 `initialize` 握手（协议版本协商）—— ⚠ 见下方【修正 ②】
3. 发 `tools/list` 取回工具清单
4. 逐个 `ToolRegistry.register(name, description, schema, handler, risk_level)`
5. 子进程常驻；Agent 触发调用时把 LangGraph 的 `tool_call` 翻译成 JSON-RPC `tools/call`

> **⚠ v4.1 修正 ②：第 2 条对「默认路径」仍然成立，但说法不完整 —— 协议现在有两代。**
>
> | 代际 | 版本 | 行为 | 谁走这条 |
> |---|---|---|---|
> | **Legacy** | ≤ `2025-11-25` | 发 `initialize` 握手协商 | **低层** `ClientSession.initialize()`；**高层** `Client(mode="legacy")`。实测谈定 = **`2025-11-25`** |
> | **Modern** | `2026-07-28` 起 | **取消握手**，版本 / 身份 / 能力改走**逐请求的 `_meta`**（规范原文 *"There is no negotiation handshake"*） | **高层 `Client` 的默认 `mode="auto"`** —— 先探 `server/discover`，成功即谈定 `2026-07-28` |
>
> - ⚠️ **关键区分**：SDK 里 `LATEST_PROTOCOL_VERSION = 2026-07-28`，而 `LATEST_HANDSHAKE_VERSION = 2025-11-25`
>   —— **「最新的一代」和「默认走的那一代」是两个不同的值**。**规范里有什么 ≠ 实现默认用什么**，后者只能靠实测得到。
> - ⚠️ **2026-10-02 D26 补充（本文档上一版的口径被这次实测推翻）**：上一版把「**stdio 默认走握手代**」写成了通例。
>   抓包对照（`scripts/d26_verify.py` F 段：同一个 server、同一个 Client，只改 `mode`）证明它**只对低层与 legacy 路径成立**：
>   **高层 `Client` 的默认 `mode="auto"` 根本不发 `initialize`**，第一封报文是 `server/discover`，谈定 `2026-07-28`。
>   → 正确说法是 **「默认走哪一代，取决于用哪一层客户端」**，而不是「默认走握手代」。
> - **双重实证（legacy 侧）**：本机 Python 自研 server 与 **Harness 官方 Node server，在 legacy 路径下都谈定 `2025-11-25`**
>   → 这一代的行为**不是某一家 SDK 的怪癖**。
> - **本项目不手写协商**，交给官方 `Client`（自带代际探测与自动回退，`mode="auto"`）→ **本项目实际落在 Modern 代**。

**四个必须处理的工程细节**：

| # | 细节 | 对策 |
|---|---|---|
| 1 | stdio = 常驻子进程，非 HTTP | 实现生命周期管理：启动/健康检查/**崩溃重启（退避）**/退出清理，防子进程泄漏 |
| 2 | 首次启动慢（拉 npm 包 + 可选下载 ~23MB ONNX 模型） | `scripts/warmup_mcp.py` 预热；**锁版本**；演示前必跑 |
| 3 | 写操作风险分级 | 阶段 1 默认只读（`none` 或 `low_write`）；F6.11 显式确认门 |
| 4 | **42** 个 toolset 全开会塞满上下文 | 按需裁剪（F6.10）。官方理由：**工具数膨胀降低 LLM 选型准确率** |

---

#### 9.6.3 v4.1 修正汇总（五处，2026-10-01 / 10-02）

本节原按 2026-09-22 的官方 README 写成，**D25/D26 实测后多处已不成立**。集中列在这里，方便一处看全：

| # | 原写 | 实测事实 | 改法 |
|---|---|---|---|
| **①** | `from fastmcp import FastMCP` | **`mcp` 2.2.0 下直接 `ImportError`**；官方已把 `FastMCP` 改名为 `MCPServer`，`fastmcp` 这个名字现在是 **PrefectHQ 的独立第三方框架** | 改为 `from mcp.server import MCPServer`；**代码里出现 `from fastmcp import FastMCP` 即为跑偏** |
| **②** | 「stdio 发 `initialize` 握手」 | **只对低层 `ClientSession` 与 `Client(mode="legacy")` 成立**（实测谈定 `2025-11-25`）；**高层 `Client` 默认 `mode="auto"` 不发 `initialize`** —— 首封报文是 `server/discover`，谈定 `2026-07-28`（2026-10-02 抓包修正，详见 **⑤**） | **保留原句 + 补一代与适用边界**，不删；**但不许再把「握手」写成通例默认** |
| **③** | 252 资源类型 / 41 toolsets / 35 prompt 模板 | **259 / 42**，版本 **3.2.31**；官方**文档页**写的 139 / 30 也已过时；**prompt 模板数本次未测** | 改数字；**并注明"资源类型数需带动词口径"**（各动词 174~198 不等） |
| **④** | 「生成**只读** PAT」 | **个人 PAT 做不到只读** —— 官方原文 *"API keys and their tokens inherit the permissions of the account under which they are created"*，创建过程里没有只读选项 | 验收口径改为「**PAT + 服务端 `HARNESS_READ_ONLY=true`**」；另注：本账号 `admin=false`，**「建只读服务账号」这条路走不通** |
| **⑤** | 「stdio **默认**走握手代 `2025-11-25`」（= 本文档上一版对 ② 的表述） | **高层 `Client(mode="auto")` 默认走 Modern 代**（`2026-07-28`，**无握手**）；只有 legacy 路径才发 `initialize`。证据：同一个 server、同一个 Client，只改 `mode` → 报文序列 `['server/discover','tools/list']` vs `['initialize','notifications/initialized','tools/list']` | 改成「**默认走哪一代，取决于用哪一层客户端**」。★ 这是"**把一个在特定路径上成立的观测，写成了通例**"的第二次同族犯错（第一次是 10-01 的「规范版本 ≠ 实现默认路径」） |

**取证位置**：
- ①③④ 与 Harness 相关部分 → `docs/tutorials/README.md` 的「🔌 Harness 接入实测」段（含证据链、259/42 定案、路由实证、错误语义、账号到期风险）。
- ②⑤ 代际与默认路径 → **可复现**：`uv run python -m scripts.d26_verify` 的 **F 段**（抓包对照 `mode="auto"` / `mode="legacy"`，
  F0 是一条守门断言，确保引用的报文没被截断）。

> **⚠️ 修正 ③ 的教训值得单独记**：同一个「规模」被**三个来源**写成三个不同的数（PRD 252 / 官方文档页 139 / 实测 259）。
> 三份都自称权威 —— **只有一份是实测**。→ **凡引用外部系统的规模数字，必须标明"读取日期 + 来源层级（文档 / 实测）"**，
> 否则无法判断谁该覆盖谁。

> **⚠️ 修正 ④ 的教训**：「只读」这个词被放在**错误的层**上（凭证层），而它其实在**服务端层**。
> 这类"**能力挂在哪个层**"的错**不会报错** —— 拿着全权 token 去调只读接口，测试照样全绿。
> → 写风控设计时，每一道护栏都要写明它**实施在哪个进程**。

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
    """按口径查询系统运行指标。可用指标名与维度见 metrics.yaml。

    Args:
        metric: 指标名，如 eval_accuracy
        dimension: 分组维度，如 config_name / category / span_name
        start_date / end_date: YYYY-MM-DD
    """
    spec = METRIC_REGISTRY[metric]                       # 语义层加载
    if dimension and dimension not in spec.dimensions:
        raise ValueError(f"不支持的维度: {dimension}")    # 白名单校验 → 注入防护

    if spec.source == "langfuse_api":                    # 🆕v4.1 第二数据源（运行追踪）
        rows = langfuse_client.query(spec, dimension, start_date, end_date)
    else:                                                # source == "sql"（项目库）
        sql = render(spec.sql_template, dimension=dimension)   # 模板渲染，非自由生成
        rows = db.fetch_all(sql, {"start": start_date, "end": end_date})

    return {"metric": metric, "dimension": dimension, "rows": rows}   # 结构化返回
```

**三层防护**：
1. **指标名白名单** —— 不在 `METRIC_REGISTRY` 中直接拒绝
2. **维度白名单** —— 不在 `spec.dimensions` 中直接拒绝（`{dimension}` 只能取白名单值）
3. **时间参数绑定** —— 走参数化查询，不做字符串拼接

#### 9.10.3 语义层文件（`metrics.yaml`）— 🆕v4.1 全部换为系统自身指标

> **v4.1 修正**：v4.0 此处的示例指标是库存业务指标（`inventory_turnover` / `inventory_flow` / `stock_level`），**与项目底座（RAG 知识库）业务语境不符**。v4.1 全部替换为**系统自身运行指标**——数据天然存在、业务自洽。
>
> 完整的数据资产盘点见 `docs/可分析数据资产清单-2026-09-22.md`。**每个指标都必须能在该清单中找到来源说明**（F11.8）。

```yaml
metrics:
  # ══ 数据源一：项目 PostgreSQL（source: sql）═══════════════════

  eval_accuracy:                     # ★ 趋势 + 消融对比（链 1 第①跳）
    label: 评测准确率
    description: judge 得分 ≥4/5 的用例占比，按检索配置分组
    source: sql
    table: eval_runs
    time_column: created_at
    dimensions: [config_name]        # pure_vector / hybrid / hybrid_rerank
    sql_template: |
      SELECT {dimension} AS dim,
             AVG(accuracy)::float AS value,
             COUNT(*) AS runs
      FROM eval_runs
      WHERE created_at >= :start AND created_at < :end
      GROUP BY {dimension}
      ORDER BY value DESC

  eval_dimension_scores:             # 定位最弱维度
    label: 评测维度得分
    description: 正确性 / 引用忠实度 / 完整性 三维度均分
    source: sql
    table: eval_runs
    time_column: created_at
    dimensions: [score_type]         # correctness / faithfulness / completeness
    sql_template: |
      SELECT score_type AS dim, AVG(score)::float AS value
      FROM (
        SELECT 'correctness'  AS score_type, score_correctness  AS score, created_at FROM eval_runs
        UNION ALL
        SELECT 'faithfulness' AS score_type, score_faithfulness AS score, created_at FROM eval_runs
        UNION ALL
        SELECT 'completeness' AS score_type, score_completeness AS score, created_at FROM eval_runs
      ) t
      WHERE created_at >= :start AND created_at < :end
      GROUP BY score_type

  eval_failures_by_category:         # ★ 明细指标，下钻用（链 1 第②跳）
    label: 分类失分
    description: 按题目类别聚合的失分情况，用于定位最弱的题型
    source: sql
    table: eval_case_results         # ⚠️ v4.1 新增表（见 §10）
    time_column: created_at
    dimensions: [category]           # doc_qa / cross_doc / tool_call / multi_step
    sql_template: |
      SELECT c.category AS dim,
             AVG(r.score_correctness)::float  AS correctness,
             AVG(r.score_faithfulness)::float AS faithfulness,
             COUNT(*) FILTER (WHERE NOT r.passed) AS fail_count
      FROM eval_case_results r
      JOIN eval_cases c ON r.case_id = c.id
      WHERE r.created_at >= :start AND r.created_at < :end
      GROUP BY c.category
      ORDER BY correctness ASC

  doc_processing_success:            # 文档入库健康度
    label: 文档处理成功率
    description: 入库成功的文档占比，按文件类型分组
    source: sql
    table: documents
    time_column: created_at
    dimensions: [file_type, status]
    sql_template: |
      SELECT {dimension} AS dim, COUNT(*) AS value
      FROM documents
      WHERE created_at >= :start AND created_at < :end
      GROUP BY {dimension}
      ORDER BY value DESC

  chunk_quality:                     # ★ 明细指标，下钻用（链 1 第③跳 = 根因层）
    label: 切片质量
    description: 按文档统计的切片长度分桶，用于定位分块问题
    source: sql
    table: document_chunks
    time_column: (join documents.created_at)
    dimensions: [document_id]
    sql_template: |
      SELECT d.filename AS dim,
             COUNT(*) FILTER (WHERE length(c.content) < 50) AS too_short,
             COUNT(*) FILTER (WHERE length(trim(c.content)) = 0) AS empty,
             COUNT(*) AS total,
             ROUND(100.0 * COUNT(*) FILTER (WHERE length(c.content) < 50)
                   / NULLIF(COUNT(*),0), 1) AS too_short_pct
      FROM document_chunks c
      JOIN documents d ON c.document_id = d.id
      WHERE d.created_at >= :start AND d.created_at < :end
      GROUP BY d.filename
      ORDER BY too_short_pct DESC

  # ══ 数据源二：Langfuse API（source: langfuse_api）🆕v4.1 ══════

  latency_p95:                       # ★ 各阶段性能瓶颈
    label: 各阶段延迟 P95
    description: 按 span 名称统计的 P95 耗时（秒），用于定位性能瓶颈
    source: langfuse_api
    endpoint: /api/public/observations
    time_column: startTime
    dimensions: [name]               # embedding / vector_search / bm25 / rerank / generation
    query: |                         # 由 langfuse_client 翻译为 API 查询参数（非 SQL）
      aggregate: p95
      field: latency
      group_by: name
      limit: 2000

  token_cost:                        # ★ 成本归因
    label: Token 成本
    description: 按模型统计的 token 消耗与估算成本
    source: langfuse_api
    endpoint: /api/public/observations
    time_column: startTime
    dimensions: [model]
    query: |
      aggregate: sum
      field: usage.totalTokens
      group_by: model
```

> **语义层是 Agentic BI 的公认前置条件**——它是"口径"的唯一来源，也是 Agent 与数据源之间的一层翻译。
>
> **v4.1 关键观察**：`dimensions` 白名单对**系统数据比对业务数据更自然**——维度就是表里的枚举列（`config_name` / `status` / `file_type` / `category` / `name`），**白名单边界清晰、无需人为设计**。这是"分析自己"比"分析虚构业务"更干净的又一个证据。
>
> **v4.1 边界说明**：`messages.tool_calls` 是 JSONB，聚合需 `jsonb_array_elements` 展开。**首版语义层不纳入 JSONB 指标**——语义层的价值在于"模板固定、只换维度"，JSONB 展开会让模板难读且易错。工具调用排行优先走 Langfuse（`tool_call` span 已由 F8.2 埋点覆盖）。

#### 9.10.5 双数据源设计（🆕v4.1 新增）★

**问题**：语义层的 `sql_template` 只能查 PostgreSQL，但**最有运维价值的延迟/token 数据在 Langfuse 里**（Langfuse 有自己的数据库）。若不解决，语义层只能覆盖一半的数据资产。

**设计**：指标定义增加 `source` 字段，`metrics_service` 按 source 分派：

```
query_metric(metric, dimension, start, end)
        │
        ├── spec.source == "sql"           → render(sql_template) → db.fetch_all()
        │
        └── spec.source == "langfuse_api"  → langfuse_client.query(spec) → Langfuse REST API
        │
        └──→ 统一返回 {"metric", "dimension", "rows"}   ← 上层（Agent / 前端）无感知
```

**四条设计约束**：

| # | 约束 | 理由 |
|---|---|---|
| 1 | **两条路径返回结构完全一致**（同一个 dict schema） | Agent 与 ECharts 不应感知数据来自哪里 |
| 2 | **Langfuse 只读，不双写落库**（红线 7） | 权威存储是 Langfuse；双写必然不一致 |
| 3 | **Langfuse 不可达时只降级该指标** | 不能因为外部系统挂了就让整个分析页不可用 |
| 4 | **维度白名单对两种数据源同样生效** | 注入防护与"模型不能自由查询"的原则不因数据源而放松 |

**这个设计本身就是面试素材**：被问"你的语义层怎么扩展新数据源"时，答案是"**加一个 source 分支，上层零改动**"——这正是分层设计的价值展示。

#### 9.10.4 多步分析编排（场景 F 的实现）— 🆕v4.1 改为系统数据

```
用户："最近三次评测，准确率趋势怎么样？哪个维度最弱、为什么？"
 ① plan 节点 → 选 query_metric(eval_accuracy, dimension=config_name)
 ② observe → 得到 [pure_vector 79%, hybrid 84%, hybrid_rerank 88%]
 ③ plan 节点（第二轮）→ LLM 判断："配置对比有了，但要知道哪个维度弱"
    → 选 query_metric(metric=eval_dimension_scores)
 ④ observe → faithfulness 恒为三维度最低
 ⑤ plan 节点（第三轮）→ LLM 判断："需要知道是哪类题拖低的"
    → 选 query_metric(metric=eval_failures_by_category)
 ⑥ observe → cross_doc（跨文档推理）类失分最多
 ⑦ plan 节点（第四轮）→ LLM 判断："失分是检索问题还是分块问题？"
    → 选 query_metric(metric=chunk_quality, dimension=document_id)
 ⑧ observe → 这 5 条题引用的文档超短切片占比 62%
 ⑨ answer → 结论（"问题在分块策略，不在检索算法"）+ 图表数据 + 数据出处
```

**①～⑧ 之间没有人类介入**，是 Agent 自己决定要往下查四轮——这就是"自主多步"，也是 Agentic BI 与上一代 ChatBI 的真正分界。`analysis_trace` 记录完整调用序列，供评测（F7.6 `sequence_ok`）判定。

> **为什么这条链比库存版更有说服力**：它是一条**真实的归因链**——从"准确率不够高"一路追到"分块参数不对"，而且每一跳的下一步都是由上一跳的结果决定的（不是预先写死的脚本）。面试官问"你怎么证明它真在下钻而不是按固定流程走"，这条 trace 就是答案。
>
> **依赖**：第⑥跳依赖 `eval_case_results`（v4.1 新增表），第⑧跳依赖 `document_chunks`（已建表）。**缺 `eval_case_results`，链会在第⑥跳断掉。**

### 9.11 平台对照评测（v4.0 新增 / **v4.1 加固公平性前提**）★

**目的**：把 §2.3"为什么自建"从论证表变成实测数据。

#### 9.11.1 公平性四前提（🆕v4.1 新增，缺一不可）

> v4.0 只写了"怎么跑"，没写"**怎么比才公平**"。没有这四条，对照结论站不住脚——而且面试官一追问就会塌。

| # | 前提 | 不满足的后果 | 落地方式 |
|---|---|---|---|
| **1** | **文档同源** ★ | **对照结论直接作废**——两边喂的文档不同，量纲都不一样 | 同一批文档**同时入库两侧**；Dify 知识库与 AgentForge 的 `documents` 表必须是同一组文件 |
| **2** | **Dify 侧须调优后参赛** | 变成"**调优过的打没调优的**"，赢了也是自欺 | 给 Dify 也做一轮参数调优：`top_k` / score 阈值 / reranking 模型各试一次，**用它的最优配置**参与对照 |
| **3** | **只比答案层，不比检索层** | 声称了做不到的事 | 见 §9.11.3 的"可比 / 不可比"清单 |
| **4** | **明确砍 D37 的代价** | 进度超支时随手砍掉，却不知道砍掉了什么 | 砍掉 D37 = §2.4 第 4 项差异化退回成论证表（见 §13.2 红线 6） |

> ⚠️ **前提 1 的现状缺口**：当前 Dify 知识库是"33 题 → 约 35 块"的实验数据，而 AgentForge 评测集是 60 条。**两者不同源，必须先统一再对照。** 这件事在 D37 当天第一件事就要做。

#### 9.11.2 执行流程

```
① 前置：同一批文档入库两侧（前提 1）+ Dify 参数调优（前提 2）
        ↓
② 30 条同题（跨 A/B/C/D 类，与 AgentForge 评测集同源）
   ├── AgentForge 作答（可配置检索链路，用最优配置）
   └── Dify 应用作答（走已发布的 Dify API，用它调优后的配置）
        ↓
③ 同一个 judge（同 prompt、同口径、temp=0）—— 由 F7.7 外部答案源支持
        ↓
④ docs/eval-compare-{date}.md
```

**Dify 侧调用链**（已验证可用）：`X-App-Code` → `/api/passport` → `X-App-Passport` → `/api/chat-messages`

#### 9.11.3 可比 / 不可比清单（🆕v4.1 新增，重要）

**这条必须写进报告，否则等于声称了做不到的事。**

| 层级 | 可比性 | 说明 |
|---|---|---|
| 端到端答案质量（judge 三维度、准确率） | ✅ **可比** | 两边都有最终答案，同一 judge 可打分 |
| 失败模式分类 | ✅ **可比** | 对失败案例人工/半自动归类 |
| 单类问题的相对强弱 | ✅ **可比** | 按 A/B/C/D 类分组比较 |
| **检索召回率 / MRR / NDCG** | ❌ **不可比** | **Dify 不暴露 chunk 级检索结果**，拿不到它的召回集 |
| **消融实验（S2：混合+重排 vs 纯向量）** | ❌ **不可比** | 这是 AgentForge 内部的能力，Dify 侧无对应配置维度 |
| 延迟 / 成本 | ⚠️ 参考值 | 两边部署环境不同（本机 vs Docker），只能作参考不作结论 |

> **诚实结论**：对照报告能回答的是"**谁的答案更好、差在哪类问题上**"，**不能**回答"谁的检索算法更好"。后者需要 Dify 开放检索中间结果，它不开放。

#### 9.11.4 报告要回答的问题

1. 两者总体准确率差异多少？
2. 差异集中在哪一类问题？（预期：跨文档推理与多步分析类差距最大——因为 Dify 检索黑盒、无 RAG 配置可调）
3. 各自的失败模式是什么？（Dify：检索召回失败且无法定位原因；AgentForge：可控，可做消融归因）
4. 成本对比：自建的复杂度代价 vs 可控性收益
5. **🆕v4.1 免责声明**：明确标注本次对照的可比范围（只到答案层），并列明 Dify 侧的配置，供读者判断公平性

> ⚠️ **前提**：必须先有评测体系（D20–D24）。没有 judge 与评测集，这件事做不了。

### 9.12 Langfuse 取数通道（🆕v4.1 新增）★

> 功能需求见 F8.5。本节是模块设计。**这是 v4.1 补上的唯一一条缺失链路。**

#### 9.12.1 为什么必须补

v3.0/v4.0 的 F8 只做了**单向写入**（埋点 → Langfuse 面板）。但 Langfuse **有自己的数据库**，数据不在项目 PostgreSQL 里。结果是：

| 数据 | 在 v4.0 里 Agent 能否分析 | 原因 |
|---|---|---|
| 评测准确率、文档切片质量、对话量 | ✅ 能 | 在项目库里，SQL 可查 |
| **延迟 P95、token 成本、各段耗时、MCP 调用成功率** | ❌ **不能** | **在 Langfuse 里，且没有回流通道** |

**即：最有运维价值的那一半数据是孤岛。** 补上通道后，"分析系统自身运行数据"才完整——**结构化数据走 SQL，运行追踪走 Langfuse API。**

#### 9.12.2 模块接口（`app/services/langfuse_client.py`）

```python
class LangfuseClient:
    """Langfuse 只读取数客户端。凭据只从 .env 读（红线 4）。"""

    def __init__(self, host: str, public_key: str, secret_key: str) -> None:
        ...

    async def query(self, spec: MetricSpec, dimension: str | None,
                    start: str, end: str) -> list[dict]:
        """按指标定义查询 Langfuse，返回与 SQL 源【同构】的 rows。

        维度白名单在此仍然生效：dimension 不在 spec.dimensions 内直接拒绝。
        """
        if dimension and dimension not in spec.dimensions:
            raise ValueError(f"不支持的维度: {dimension}")

        observations = await self._fetch_observations(
            from_ts=start, to_ts=end,
            limit=spec.query.get("limit", 1000),
        )
        return self._aggregate(
            observations,
            agg=spec.query["aggregate"],     # p95 / sum / avg
            field=spec.query["field"],       # latency / usage.totalTokens
            group_by=dimension,
        )

    async def _fetch_observations(self, from_ts, to_ts, limit) -> list[dict]:
        # GET {host}/api/public/observations （分页拉取，带 60s 短期缓存）
        ...
```

#### 9.12.3 四条设计约束

| # | 约束 | 理由 |
|---|---|---|
| 1 | **返回结构与 SQL 源同构**（同一个 dict schema） | Agent 与前端 ECharts **不应感知数据来自哪里**（F11.7） |
| 2 | **只读，不双写落库**（红线 7） | 权威存储在 Langfuse；双写必然导致不一致 |
| 3 | **不可达时只降级该指标** | 外部系统挂了不能让整个分析页不可用；Agent 应能说明"该指标暂不可用" |
| 4 | **维度白名单同样生效** | "模型不能自由查询"的原则不因数据源而放松 |

#### 9.12.4 工程注意

- **分页与限流**：Langfuse API 有分页，`observations` 在大时间窗下数据量大，需限制 `limit` 并做 60s 短期缓存。
- **时间字段**：Langfuse 用 `startTime`（非 `created_at`），语义层的时间绑定需按数据源区分。
- **无 Embedding 依赖**：此通道不需要向量库，只是 HTTP 只读查询，**不影响检索链路**。

#### 9.12.5 面试素材

> 被问"你的可观测体系除了看板还做了什么"时，答案是：**"我把追踪数据做成了可分析对象——Agent 能用自然语言问'最近哪一段慢'，因为语义层支持两种数据源，SQL 和 Langfuse API 在上层是同一个接口。"** 这比"我接了 Langfuse 看板"高一个层级：**看板是给人看的，回流是给 Agent 用的。**

---

## 10. 数据模型

**v4.0 新增 1 张表（`mcp_servers`）+ v4.1 新增 1 张表（`eval_case_results`）**，其余沿用 v3.0。

```sql
-- ↓↓↓ 原有表（不变）：sessions / messages / documents / document_chunks / eval_cases / eval_runs ↓↓↓
-- 说明：定义见 docs/PRD.md §10。三条关键约束保留：
--   1) document_chunks 的倒排行用 tsvector + GIN，BM25 打分在应用层自算
--   2) 分词列 content_tokens 由 jieba 生成，索引侧与查询侧必须同一分词器
--   3) embedding 用 hnsw + vector_cosine_ops

-- 评测用例：category 增加 multi_step，并新增调用序列字段
ALTER TABLE eval_cases ADD COLUMN expected_call_sequence JSONB;   -- 🆕 ['query_metric','query_metric']
-- category 取值扩展为：doc_qa | cross_doc | tool_call | multi_step

-- 🆕v4.1 评测单用例明细（v4.1 新增，缺失的表）
CREATE TABLE eval_case_results (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES eval_cases(id) ON DELETE CASCADE,
    answer TEXT,                        -- 该用例的系统实际答案
    score_correctness FLOAT,
    score_faithfulness FLOAT,
    score_completeness FLOAT,
    sequence_ok BOOLEAN,                -- 多步题：调用序列是否合理（F7.6）
    passed BOOLEAN,                     -- judge 得分 ≥4/5
    failure_reason TEXT,                -- 失败归因（供报告"失败案例各取 3 条"）
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (run_id, case_id)
);
CREATE INDEX idx_ecr_run ON eval_case_results(run_id);
CREATE INDEX idx_ecr_case ON eval_case_results(case_id);

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
| GET | /api/metrics | 🆕 指标语义层列表（**🆕v4.1 响应含 `source`**） | - | `[{name,label,description,source,dimensions}]` |
| POST | /api/metrics/query | 🆕 直接查询指标（调试用；**🆕v4.1 按 `source` 分派 SQL 或 Langfuse**） | `{metric, dimension?, start_date, end_date}` | `{rows:[...], source}` |
| GET | /api/observability/status | **🆕v4.1 Langfuse 连通性与近期数据量**（供 §17.3 第 8 项自检） | - | `{reachable, recent_spans, host}` |
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
| 可观测 | 100% 对话有 Langfuse trace；错误有结构化日志；**🆕 MCP 工具调用可单独辨识来源**；**🆕v4.1 追踪数据可经只读通道回流，供 Agent 分析（延迟/token/各段耗时）** |
| 可维护 | 三层解耦；prompt 集中管理；核心链路有测试 |
| 可扩展 | 检索策略配置化；工具注册制；LLM 通道可加；**🆕 指标口径集中在 metrics.yaml；🆕 新增 MCP server 无需改 Agent 代码**；**🆕v4.1 语义层新增数据源只需加一个 `source` 分支，上层零改动** |
| **健壮性** 🆕 | **MCP 子进程崩溃可自愈；外部 server 不可用时 Agent 降级为纯 RAG 回答，不阻断主流程**；**🆕v4.1 Langfuse 不可达时只该指标报错，SQL 类指标不受影响** |

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
| | D21 | 评测集 **50 条基础题**（A30/B10/C10）+ API | /api/eval/cases ok |
| | D22 | LLM-as-judge 评分器 + **🆕v4.1 明细落库 `eval_case_results`** | 单条可评分；**🆕 明细表有行、按类别可聚合** |
| | D23 | 消融脚本 3×**50** 自动跑 | 3 配置全跑通 |
| | D24 | 报告生成（失败案例抽样依赖 D22 明细表） | 对比表+失败案例 |
| **M5 MCP+观测** | D25 | 学 MCP 协议（重点学习日）+ **🆕 注册 Harness 免费账号、建最小流水线、生成 PAT**（**⚠ 修正 ④**：「只读」不在 PAT 上 —— 个人 PAT 无只读档，靠服务端 `HARNESS_READ_ONLY=true`） | 能讲清 MCP 价值；**PAT 已入 .env** ✅ 2026-10-01 |
| | D26 | **自研 MCP 库存服务**（官方 SDK `MCPServer`；原写 FastMCP → **修正 ①**） | 服务独立可调（客户端 `tools/list` 能列出工具、`tool_call` 能取数） |
| | D27 | MCP 客户端接入（**含 🆕 外部 Server 生命周期 + 注册 Harness + toolset 裁剪 + 只读风控**） | 对话可调库存工具；**🆕 /api/tools 可见 11 个 harness 工具** |
| | D28 | 三层埋点补全（**🆕 含 MCP span 与来源标识**）+ **🆕v4.1 Langfuse 取数通道 `langfuse_client.py`（F8.5）** | 全链路可追踪；**🆕 能指出某次调用来自 harness**；**🆕v4.1 能经 API 拉回 observations** |
| **M6 UI（React）** | D29 | React 脚手架（Vite+TS+AntD）+ API client | dev server 起，/health 打通 |
| | D30 | ChatPage：消息流 + SSE 打字机 + 引用高亮 + **🆕 ToolCallCard 工具卡片** | 场景 A/B/C 浏览器可演示；**🆕 工具卡片显示来源 server** |
| | D31 | KnowledgePage + EvalPage + 会话侧栏 | 场景 D 可触发评测看报告 |
| **M7 工程化** | D32 | Docker Compose（+web 容器，**🆕 含 Node 运行时**）+ pytest + **🆕 warmup_mcp.py 预热** | 一键起，测试过 |
| | D33 | README/架构图/演示脚本（**🆕 含 MCP 接入说明与凭据配置指引**） | 5 分钟 demo 可复现 |
| | D34 | 面试三件套验收（评测报告/追踪/MCP） | 全达标（**🆕 S7 由 S10 的真实外部工具满足**） |
| **M8 前沿技术** 🆕 | **D35** | **🆕 语义层 `metrics.yaml`（🆕v4.1 换为 6 个系统自身指标）+ `metrics_service.py`（含**双数据源分派**）+ `query_metric` 工具 + 三层防护** | **指标可查；注入用例全拦截；🆕v4.1 SQL 与 Langfuse 两类指标均可返回** |
| | **D36** | **🆕 多步分析编排（prompt + analysis_trace）+ AnalyticsPage + ECharts + 评测集类型 D（10 条）** | **场景 F（🆕v4.1 系统数据、多跳下钻）出图+结论；多步题 ≥70% 通过** |
| | **D37** | **🆕v4.1 当日先做两个前置：文档同源入库两侧 + Dify 参数调优；再做 Dify 对照评测（dify_bridge + eval_compare）+ 对照报告** | **产出 eval-compare-{date}.md（含可比范围免责声明）** |
| **M9 缓冲** | D38-42 | 缓冲（超支消化，最多 5 天） | 不推新功能 |
| | | **总周期上限 42 天（含缓冲）** | |

**红线（原有 5 条不变 + v4.0 追加 2 条 + v4.1 追加 1 条）**：
1. 每天 3-5h 是总预算，学习消化时间计入，不硬赶
2. 当天验收不过 → 只修当天，不推新
3. D19 后冻结后端新功能（P1 全部砍掉，保底 P0）
4. UI 复用用户 React 经验，不安排"学前端"时间，只排"对接+打磨"
5. 缓冲天只用于补进度，不用于加需求
6. **🆕v4.0 D37 是"可选阻断点"——若进度超支，D37（Dify 对照评测）最先砍，因为它不影响其他任何模块的正确性**
   - **🆕v4.1 补充：砍它要知道砍掉什么。** D37 承载 §2.4 差异化来源第 4 项（平台对照评测方法论）。砍掉后，§2.3"为什么自建"**从实测数据退回成论证表**——不是不能砍，是砍了要接受这个降级。
7. **🆕v4.0 D35–D37 不得提前到 D28 之前**——三项技术都依赖评测体系（D20–D24）与 MCP 客户端（D27）先就位
8. **🆕v4.1 D35 是重载日，允许拆两天**——D35 同时含"6 个系统指标 + 双数据源分派 + `query_metric` + 三层防护"，工作量偏大；**若当天验收不过，允许拆为两天（从缓冲借 1 天）**，不得以"先做个简单的"为名砍掉三层防护（那是 F11.4 的验收项）

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
- **🆕 MCP 层用 `MockMCPServer`**（本地自研 `MCPServer` 假 server，**修正 ①**）测客户端逻辑，避免 CI 依赖外部网络与 PAT
- **🆕 Harness 侧集成测试标记为 `@pytest.mark.external`**，默认跳过，本地手动执行

---

## 15. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| LangGraph API 学习曲线 | 中 | 高 | 只用最简 ReAct 图；先 AgentExecutor 兜底 |
| bge-reranker 部署成本 | 中 | 中 | 本地 ONNX 或 API；失败降级不重排 |
| 评测集质量影响可信度 | 中 | 高 | 3 类分布固定；judge 规则写死；人工抽查 10% |
| MCP SDK 版本兼容 | 中 | 中 | 锁版本；用官方 `MCPServer` 最小实现（**修正 ①**：不用第三方 `fastmcp`，见 §9.6.1） |
| 时间超支 | 高 | 高 | P1 全砍；每日红线；D19 冻结；5 天缓冲兜底 |
| API key 泄漏 | 中 | 高 | .env 不入 git；用户重置 DeepSeek key |
| **🆕 D10 欠账未补** | **高** | **高** | **数据丢失风险（Redis 单层，>8 轮/>24h/重启即丢）。补账优先级最高，建议在 D14 前插入或与 D14 合并** |
| **🆕 Harness MCP Server 迭代过快** | **高** | **中** | **锁版本号（不用 `@latest`）；规模数字引用前必读 README；PAT 失效时降级为仅自研工具** |
| **🆕 Node 子进程泄漏** | **中** | **中** | **生命周期管理 + 退出钩子 + 测试覆盖崩溃重启；`warmup_mcp.py` 统一管理** |
| **🆕 首次启动慢导致演示翻车** | **高** | **中** | **演示前必跑预热脚本；Docker 镜像预装 npm 包** |
| **🆕 多步分析失控（无限下钻）** | **中** | **中** | **🆕v4.1 调整：`recursion_limit=12`（v4.0 的 10 在 4 跳链下仅剩 1 余量）+ prompt 明确"最多下钻 4 轮"；超限须输出已得发现而非空转；analysis_trace 可审计** |
| **🆕 语义层设计过复杂** | **中** | **中** | **D35 只做 §9.10.3 的 6 个指标；扩展留 P1；不追求通用 BI 语义层** |
| **🆕 Harness 免费档额度变动** | **中** | **低** | **已记录 2026-08-28 一次削减；若不足以支撑演示，退回纯自研工具并保留文档说明** |
| **🆕v4.1 对照评测不公平（两边文档不同源）** | **中** | **高** | **D37 当日第一件事就是文档同源入库两侧；报告须列出两侧配置与文档清单供核查** |
| **🆕v4.1 对照评测被质疑"给 Dify 没调优"** | **中** | **中** | **前提 2 强制：先给 Dify 调 top_k / score 阈值 / reranking，用它最优配置参赛；报告写明配置** |
| **🆕v4.1 Langfuse API 不可达或限流** | **中** | **中** | **F8.5.4 降级：只该指标报错，SQL 类指标不受影响；60s 短期缓存减轻调用压力** |
| **🆕v4.1 系统自身数据量小，趋势无说服力** | **高** | **中** | **demo 级规模，评测跑 3 次仅 3 个点。演示应强调「配置对比」（三配置横比）而非「时序」；消融 A/B/C + 复跑可造出 3–6 个点。被问数据量时如实说明** |
| **🆕v4.1 分析对象是系统自身，被质疑"自娱自乐"** | **中** | **中** | **准备标准答法：AI 平台的运营者需要知道自己系统跑得怎么样，这是真实需求；且这条链与项目最强项（评测体系）形成闭环，不是外挂模块** |

---

## 16. 面试叙事与演示脚本

### 16.1 30 秒电梯陈述（v4.0 升级 / v4.1 微调）

> 我做了个企业 Agent 平台 AgentForge。它不是 demo：文档上传 → 混合检索+重排的 RAG 问答 → **通过 MCP 协议接入企业级 DevOps 平台（Harness）诊断真实流水线** → **带语义层的自主多步数据分析（分析对象是系统自身的评测与运行数据）** → 滑窗+摘要记忆 → 全链路 Langfuse 可观测，**而且 Langfuse 的性能数据能回流给 Agent 分析**。最关键是评测体系：60 条评测集 + LLM-as-judge 打分 + 消融实验，数据证明混合+重排比纯向量准确率提升 10%+；**我还用同一套 judge 做了 Dify 与自建方案的对照评测，用数据说明为什么自建**。架构上 LLM Gateway、工具注册表、Agent 引擎完全解耦，可平滑升多 Agent。

### 16.2 5 分钟演示脚本（v4.0 升级 / v4.1 修正第 5 段）

1. **开场**（30s）：架构图讲分层
2. **知识库**（45s）：上传示例文档 → 状态 ready
3. **RAG 问答**（1min）：问文档问题 → 答案带引用 → 点引用看原文
4. **🆕 外部系统互操作**（1.5min）：问"我最近哪些流水线失败了，为什么" → 工具卡片显示"来源：harness" → 展开看 Agent 给出的失败分类与建议 → 切到 Langfuse 指出这次 MCP 调用的 span
5. **🆕 Agentic BI 分析**（1.5min）：问"**最近三次评测准确率趋势怎么样？哪个维度最弱、为什么？**" → 看 Agent **多跳下钻**（趋势 → 分类失分 → 切片质量）→ ECharts 出图 + 结论（"问题在分块策略"）
6. **评测**（1min）：跑消融 → 展示准确率对比报告 → **🆕 顺带展示 Dify 对照报告**
7. **🆕v4.1 可选加段：Langfuse 数据回流**（1min）：问"最近一周延迟趋势、哪一段最慢" → 走 Langfuse API → 折线图 + 结论（"重排占 44%"）

> **演示时长提示**：v3.0 脚本为 5 分钟；v4.0 加第 4、5 段后约 **6.5 分钟**；v4.1 若加第 7 段则约 **7.5 分钟**。
> **限时 5 分钟时的降级方案**：① 第 6 段的对照报告改为一句带过（"这份报告我放在文档里"）② **第 7 段砍掉**（它是 S15「应该」级指标，非必须）③ 第 5 段的三跳下钻只演两跳。

### 16.3 面试官追问防御（v4.0 扩充 / v4.1 加固）

| 追问 | 回答 |
|---|---|
| 为什么不用 Dify？ | 三段式：① 可控性（检索链路可调、可做消融）② 可评测（Dify 检索是黑盒，无法支撑 S2 的消融实验）③ **🆕 而且我实测过——我做了 30 条同题对照评测，报告在这里** |
| **🆕v4.1 你这个对照公平吗？** | **公平性我专门做了四条约束**：① **两侧入库的是同一批文档**（不同源结论无效）② **Dify 侧我做了参数调优再参赛**（top_k / score 阈值 / reranking 都试过，用它的最优配置），不是拿默认配置打调优过的自己 ③ **只比答案层**——Dify 不暴露 chunk 级检索结果，所以我没声称比过"谁的检索算法更好" ④ 报告里列了两侧配置供核查 |
| **🆕v4.1 你的可观测除了看板还做了什么？** | **我把追踪数据做成了可分析对象**。埋点是单向写入，但 Langfuse 有自己的库，所以我又写了一条只读取数通道，让语义层支持两种数据源（SQL 和 Langfuse API），上层接口完全一致。**结果是我能用自然语言问"最近哪一段慢"，Agent 会走 Langfuse API 拉回分阶段耗时并出图。看板是给人看的，回流是给 Agent 用的。** |
| **🆕v4.1 你分析的数据是哪来的？数据量够吗？** | **数据源是系统自己运行产生的**——评测结果、文档切片质量、对话记录、MCP 健康度、运行追踪。所以不需要造业务数据，跑一次评测就有。**数据量确实是 demo 级**（评测跑 3 次只有 3 个点），所以我的演示重点放在**配置对比**（三配置横比）而不是**长期时序**；消融 A/B/C + 复跑能造出 3–6 个点 |
| 检索不准怎么办？ | 展示消融实验：混合+重排提升 X%，失败案例已分析 |
| 怎么证明效果？ | 评测集 + LLM-as-judge + 失败案例分析 |
| 工具怎么扩展？ | MCP 标准协议，**不写死函数**。**🆕 实证：我加了 Harness 的 server，Agent 代码一行没改** |
| **🆕 你做过 MCP 互操作吗？** | **做过。接入了 Harness 官方 MCP Server（11 工具 × 259 资源类型），工具是运行时 `tools/list` 动态发现的；我用 toolset 裁剪控制上下文膨胀，并按风险分级把写操作限制为只读** |
| **🆕 Agent 生成的 SQL 出错了怎么办？** | **我的设计里 Agent 不写 SQL。它只能从 `metrics.yaml` 选指标名和维度，SQL 由模板渲染 + 参数绑定，维度走白名单——所以不存在"生成错 SQL"这个失败模式** |
| **🆕 多步分析和普通问答有什么区别？** | **普通问答是一次工具调用；多步是 Agent 拿到初步结果后自己决定再下钻。我的评测集类型 D 专门判"调用序列是否合理"，而不是只看最终答案** |
| 多 Agent 怎么做？ | 架构已预留：LangGraph 图嵌子图 + 工具集隔离（阶段 2 roadmap） |
| 并发性能？ | 当前单体 + Redis 缓存；阶段 3 平台化加队列/多通道 |
| **🆕 外部服务挂了怎么办？** | **MCP 子进程崩溃会退避重启；连续失败时该工具下线，Agent 降级为纯 RAG 回答，不阻断主流程。🆕v4.1 Langfuse 挂了同理——只该指标报错，SQL 类指标不受影响** |

### 16.4 话术安全边界（v4.0 新增 / v4.1 扩充，重要）

**可以说的**：
- "我在做 Agent Runtime 的核心组件：ReAct 循环、工具注册、上下文与状态管理、LLM Gateway、MCP 接入、全链路追踪"
- "我实现了一个带语义层的分析工具，避免让模型直接生成 SQL"
- **🆕v4.1**："我的 Agent 能分析**系统自身**的运行数据并给出优化建议——评测结果、切片质量、延迟分布"
- **🆕v4.1**："我把 Langfuse 的追踪数据做成了只读取数通道，让性能与成本也能被 Agent 分析"

**不要说（会被追问打穿）**：
- ❌ "我造了一个 Agent Harness" —— 被追问"权限模型、沙箱边界、预算上限、checkpoint 回滚怎么设计"时无话可说
- ❌ "我做了完整的 Agentic BI 平台" —— 被追问"多租户、审计、语义层版本管理"时无话可说
- ❌ "我的 Agent 能自己写 SQL" —— 这是风险，不是亮点
- **🆕v4.1** ❌ "**我的系统能分析任意业务数据**" —— 实际分析的只有**系统自身**数据（评测/切片/对话/追踪）。说大了会被追问"那你接个业务库演示一下"，当场穿
- **🆕v4.1** ❌ "**我和 Dify 比的是检索算法**" —— 对照**只到答案层**。Dify 不暴露 chunk 级检索结果，说比了检索就是编。**正确说法**："我比的是端到端答案质量和失败模式"

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

### 17.3 演示前置检查清单（v4.0 新增 / **v4.1 修正第 6 项、新增第 8–9 项**）

演示前必须逐项确认（**任一项不过则 demo 会翻车**）：

| # | 检查项 | 命令/方式 |
|---|---|---|
| 1 | Harness PAT 有效 | `curl -H "x-api-key: $HARNESS_API_KEY" https://app.harness.io/gateway/pipeline/api/pipelines/list` |
| 2 | MCP Server 已预热 | `python scripts/warmup_mcp.py` |
| 3 | 11 个工具已注册 | `curl localhost:8000/api/tools \| jq '.[] \| select(.source=="mcp:harness")'` |
| 4 | 示例文档已入库 ready | 知识库页确认 |
| 5 | 指标语义层可查 | `curl localhost:8000/api/metrics`（确认 6 个指标 + `source` 字段） |
| **6** | **🆕v4.1 系统自身数据已就绪** | **① 至少跑过 2 次评测（`eval_runs` 有 ≥2 行）② 上传 ≥ 10 篇文档 ③ `eval_case_results` 有明细行。否则场景 F 无结论** |
| 7 | 一份已生成的评测报告 + 一份对照报告在 `docs/` 下 | 避免现场跑评测等待 |
| **8** | **🆕v4.1 Langfuse 可访问且已有追踪数据** | `curl -u $LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY $LANGFUSE_HOST/api/public/observations?limit=1`（否则场景 H 不可演示） |
| **9** | **🆕v4.1 Dify 侧已就绪且配置已记录** | Dify 应用可调通（`X-App-Code` → passport → chat-messages）；**确认两侧知识库是同一批文档**、Dify 参数调优配置已写入报告 |

> **🆕v4.1 提醒**：第 6 项是 v4.0 那版"确认库存表有 30 天数据"的**替代项**。v4.0 那一项本质是"**为演示凭空造业务数据**"，与项目底座不符；现在改为"确认系统自身数据足够"——数据是靠正常跑评测/传文档积累的，不需要造假。
> **第 8 项若不满足**：场景 H 与第 7 段演示直接跳过（S15 是「应该」级指标），**不要临场调试**。

### 17.4 来源与依据（v4.0 新增 / v4.1 补充）

| 内容 | 来源 | 日期 |
|---|---|---|
| Harness MCP Server 规模与配置 | `github.com/harness/mcp-server` README（直读） | 2026-09-22 |
| Dify App 可暴露为 MCP Server | 本地 Dify 1.17 源码（`api/controllers/mcp/mcp.py` 等） | 2026-09-21 |
| Agentic BI 定义与语义层前置 | thoughtspot.com/glossary/agentic-bI；衡石 2026 数智化趋势 | 2026-09-21 |
| PRD 已有接入点 `POST /api/mcp/register` | `docs/PRD.md` §11（第 615 行） | — |
| 项目进度与欠账 | `docs/进度核对报告-2026-09-20.md` | 2026-09-20 |
| **🆕v4.1 系统数据资产盘点（三层结构 / 7 表 / 3 条下钻链 / 指标草案）** | **`docs/可分析数据资产清单-2026-09-22.md`** | **2026-09-22** |
| **🆕v4.1 「项目库只有 4 张表、`inventory` 零命中」的实测结论** | **代码实测：`grep __tablename__ app/models/`；`grep -rln inventory app/ mcp_servers/`** | **2026-09-22** |
| **🆕v4.1 对照评测公平性四前提（可比/不可比清单）** | **本版推导；依据：Dify 不暴露 chunk 级检索结果这一事实** | **2026-09-22** |

---

## 18. 本版遗留与开放问题（🆕v4.1 新增）

| # | 问题 | 状态 | 建议 |
|---|---|---|---|
| 1 | **D10 欠账未补**（`messages`/`sessions` 实测 0 行） | ⚠️ 未解决 | **优先级最高**：它同时是 harness 最大缺口、数据丢失风险、以及所有"对话类分析指标"的前置。建议在 D14 前插入或与 D14 合并 |
| 2 | **阶段 2/3 无独立 PRD** | ⚠️ 文档缺口 | v3.0/v4.x 只详细写了阶段 1；阶段 2/3 散见于 §6.2 / §8.2 / §16.3。若面试需要讲"演进路线"，可补一份轻量 roadmap |
| 3 | **`eval_case_results` 的 `failure_reason` 取值未定义** | ⚠️ 待细化 | D22 实现时定义枚举（如 `retrieval_miss` / `hallucination` / `incomplete` / `tool_error`），便于报告做失败模式聚类 |
| 4 | **对照评测的 30 条从哪 60 条里选** | ⚠️ 待定 | D37 当日决定；建议**每类选代表性的**（A/B/C/D 各 7–8 条），且**必须与 Dify 侧同源** |
| 5 | **`latency_p95` 的 span 命名约定未统一** | ⚠️ 待定 | 依赖 D28 埋点时的 span `name` 规范（建议固定为 `embedding` / `vector_search` / `bm25` / `rerank` / `generation`），否则分组聚合出不来 |
| 6 | **旧文档并存较多** | ⚠️ 待清理 | 目录下现有 `PRD.md`(v3.0) / `PRD-v4-含前沿技术.md`(v4.0) / `PRD-v4.1-含前沿技术.md`(本版) 三份。**建议以本版为唯一基准**，另两份加 `_已废弃_` 前缀或归档 |

---

*— PRD v4.1（含前沿技术融入 + 数据源修正 + Langfuse 回流）完 ｜ 下一里程碑：D14 = PRD D16「BM25 检索实现」（不变） —*
