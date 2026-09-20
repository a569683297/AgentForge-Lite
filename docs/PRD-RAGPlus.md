# AgentForge 阶段 1 — RAG+ 增强版 PRD

> 版本：v2.0 ｜ 日期：2026-08-28 ｜ 状态：已确认，进入实施
> 定位：4-5 周（每天 3-5h，约 100-125 工时）冲刺版，基于西安/远程真实岗位 JD 修订
> 演进关系：本阶段是 3 阶段演进路线图的**阶段 1**，完成后可增量升级到多 Agent 版（阶段 2）与完整平台版（阶段 3）

---

## 0. 相对 Lite 版的变更摘要（v1.0 → v2.0）

| 变更 | 原因（来自真实 JD 调研） |
|---|---|
| **评测体系 P1 → P0**（评测集+LLM-as-judge+消融实验） | 阿里云 20-50k 岗、华为 2012 实验室 JD 明确点名「Agent 效果评估体系建设」 |
| **新增 Langfuse 可观测性** | 海外远程岗（Protingent/Smart Working/BairesDev）必考，国内头部岗加分 |
| **MCP 从 P2 → P0**（接入真实外部工具） | 远程岗 JD 几乎全部要求 MCP 协议接入 |
| **RAG 升级为混合检索+重排** | 中坚岗（北方华鑫/昆仑联通）明确要求「检索召回率调优」「重排」 |
| **里程碑 14 天 → 35 天** | Lite 版只够「闭环」，RAG+ 版要求「有数据、有优化、有深度」 |

---

## 1. 产品定位

**一句话定位**：企业知识库驱动的智能问答助手（RAG+ 增强版）——在 Lite 版完整闭环之上，补齐**可量化效果（评测体系）、可观测（Langfuse）、可扩展（MCP 工具接入）**三大企业级能力，使项目从「进阶 demo」升级为「有工程深度的真实产品」。

**本阶段的三个「能讲故事」的交付物**：
1. **消融实验报告**：纯向量 vs 混合检索 vs +重排 的准确率对比曲线（面试时直接展示）
2. **Langfuse 追踪面板**：一次对话的完整调用链可视化（面试时演示追踪界面）
3. **MCP 真实工具接入**：通过 MCP 协议调用真实外部服务（如查库存/调 GitHub API）

---

## 2. 成功指标（验收底线）

| 指标 | 目标值 | 验证方式 |
|---|---|---|
| 评测集规模 | ≥ 50 条（3 类：文档问答30/跨文档推理10/工具调用10） | 评测集文件 |
| 消融实验提升 | 混合检索+重排 vs 纯向量，准确率提升 ≥ 10% | 评测脚本自动出报告 |
| 最终准确率 | ≥ 88% | LLM-as-judge 自动评分 |
| 端到端延迟 | P95 < 5s | 压测脚本 |
| Langfuse | 100% 对话可追踪（含工具调用、检索、LLM 调用） | 追踪面板抽查 |
| MCP | 至少 1 个真实外部工具经 MCP 协议可用 | 演示脚本 |
| 测试 | 核心链路覆盖率 ≥ 60% | pytest --cov |
| 部署 | docker compose up 一条命令 | 全新机器验证 |

---

## 3. 功能清单与优先级

### P0 — 必须（35 天内必须全部交付）
| # | 功能 | 说明 |
|---|---|---|
| F1 | 对话接口（SSE 流式） | POST /api/chat，流式返回 |
| F2 | LangGraph ReAct Agent | 规划→工具→观察→回答，5 步上限 |
| F3 | RAG 混合检索 | BM25 关键词（倒排走 tsvector/GIN，打分自算）+ 向量（pgvector）双路召回 |
| F4 | 重排 | Cross-Encoder（bge-reranker-base）重排 top5 |
| F5 | **评测体系** | 50 条评测集 + LLM-as-judge 评分 + 消融实验 + 报告生成 |
| F6 | **MCP 工具接入** | 官方 MCP Python SDK，接入 ≥1 个真实外部工具 |
| F7 | 记忆管理 | 滑窗（8 轮）+ 摘要（20 轮触发） |
| F8 | **Langfuse 可观测** | 全链路 tracing：检索/工具/LLM 调用全部落追踪 |
| F9 | 文档管理 | PDF/Word/MD/TXT 上传、切片、入库状态 |
| F10 | Web UI（Streamlit） | 对话页 + 知识库管理页 + 引用高亮 + 评测报告查看 |
| F11 | Docker Compose + pytest | 一键部署 + 核心测试 |

### P1 — 应该（有余力再做）
| # | 功能 |
|---|---|
| P1-1 | 简单登录鉴权（JWT） |
| P1-2 | 管理后台雏形（模型/工具配置页） |
| P1-3 | 更多 MCP 工具（再接入 1-2 个） |
| P1-4 | 缓存命中优化（Redis 缓存相似问题） |

### P2 — 明确不做（留到阶段 2/3，防止蔓延）
| # | 不做项 | 归属阶段 |
|---|---|---|
| P2-1 | 多 Agent 编排（Supervisor/Planner-Executor） | 阶段 2 |
| P2-2 | 消息总线/任务分解 | 阶段 2 |
| P2-3 | Agent Runtime 平台化（状态持久化/多通道 Gateway） | 阶段 3 |
| P2-4 | 管理后台完整版/可视化工作流画布 | 阶段 3 |
| P2-5 | 模型微调 / 知识图谱 | 不做 |

---

## 4. 系统架构（分层架构 — 为阶段 2/3 预留扩展点）

```
┌──────────────────────────────────────────────────────────────┐
│                    Web UI (Streamlit)                          │
│    对话页 ｜ 知识库管理 ｜ 引用高亮 ｜ 评测报告 ｜ 追踪入口      │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP/SSE
┌──────────────────────────▼───────────────────────────────────┐
│                  API 层 (FastAPI, Pydantic v2)                 │
│   /chat /documents /sessions /eval /health                     │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│              Agent 引擎 (LangGraph ReAct)  ★独立服务★          │
│   plan → [tool?] → execute → observe → ... → answer（5步上限） │
│        │                 │                                    │
│        │                 ▼                                    │
│        │      ┌─────────────────────┐                         │
│        │      │  Tool Registry ★    │ ← MCP Client → 外部工具   │
│        │      │  db_query/web_search│      (MCP SDK)          │
│        │      │  current_time/mcp_x │                         │
│        │      └─────────────────────┘                         │
│        │                                                     │
│        ▼                                                     │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐   │
│  │ RAG Engine ★ │  │ Memory Service│  │ Eval Service ★   │   │
│  │ 混合检索+重排 │  │ 滑窗+摘要      │  │ 评测集/评分/消融  │   │
│  └──────────────┘  └───────────────┘  └──────────────────┘   │
└───────────────────────────────────────────────────────────────┘
         │               │                │
         ▼               ▼                ▼
   PostgreSQL        Redis          Langfuse ★
   (pgvector+tsvector)(会话缓存)     (全链路可观测)
```

### 4.1 分层红线（阶段 2/3 零返工的前提，编码时必须遵守）

| 层 | 独立原则 | 阶段 2/3 如何复用 |
|---|---|---|
| **LLM Gateway** | 所有 LLM/Embedding 调用必须走 gateway，业务代码不得直连 SDK | 阶段 3 加多通道/降级时不动业务代码 |
| **Tool Registry** | 注册制：name/description/schema/handler 四元组，Agent 不直接调函数 | 阶段 2 多 Agent 各挂不同工具集直接复用 |
| **Agent 引擎** | ReAct 图是独立服务，通过接口注入工具与记忆 | 阶段 2 加 Supervisor 时把图嵌为子图 |
| **评测模块** | 与业务解耦，输入=配置+评测集，输出=报告 | 阶段 2 评测多 Agent 轨迹直接复用 |
| **可观测** | Langfuse 追踪埋点在 Gateway/工具/检索三层 | 阶段 3 管理后台直接消费追踪数据 |

---

## 5. 模块详细设计

### 5.1 LLM Gateway
- 统一接口：`chat(messages, model=None, stream=False)` / `embed(texts)`
- 双通道：DeepSeek（默认，便宜）+ OpenAI 兼容（备用），失败自动降级重试 1 次
- 按场景配置参数：规划（temp=0.2）/回答（temp=0.4）/摘要（temp=0.1）/评测（temp=0）
- **所有调用埋点 Langfuse**：`langfuse_context` 记录模型/耗时/token

### 5.2 Agent 引擎（LangGraph ReAct）
- 同 Lite 版结构（plan→execute→observe→answer，5 步上限）
- **升级点**：工具从「内置函数」升级为「Tool Registry 注册 + MCP 客户端混用」——同一 schema 接口，Agent 不感知工具来自本地还是 MCP

### 5.3 RAG Engine（核心升级）
| 步骤 | 实现 | 对比 Lite |
|---|---|---|
| 解析 | pypdf / python-docx / markdown | 同 |
| 切片 | 512 token + 10% 重叠，中文按字符近似 | 同 |
| 向量化 | text-embedding 系列（经 Gateway） | 同 |
| **混合检索** | **BM25 关键词（倒排走 tsvector/GIN，打分自算）+ 向量（pgvector cosine）→ RRF 融合** | ⭐ 新增 |
| **重排** | **bge-reranker-base（本地 ONNX 或 API）对 top20→top5 重排** | ⭐ 新增 |
| 生成 | 知识片段+问题，强制引用编号 [1][2] | 同 |
| **消融开关** | 配置项控制检索策略（纯向量/混合/混合+重排），供评测复用 | ⭐ 新增 |

### 5.4 评测体系（P0，全项目最差异化模块）
**设计目标**：让面试官看到「你有用数据驱动优化 Agent 的能力」——这是阿里云/华为 JD 点名的能力。

- **评测集（50 条）**：
  - 类型 A 文档问答 30 条：答案必须能追溯到上传文档（含 5 条「文档中无答案」的负例，测幻觉控制）
  - 类型 B 跨文档推理 10 条：需要融合 2 份以上文档
  - 类型 C 工具调用 10 条：需调用工具才能回答（如查库存/算日期）
- **评分器（LLM-as-judge）**：Judge 模型按 3 维度打分（0-5）：
  - 正确性（Correctness）：答案与参考答案匹配度
  - 引用忠实度（Faithfulness）：答案内容是否都有引用支撑
  - 完整性（Completeness）：是否遗漏关键信息
- **消融实验**：同一评测集跑 3 个配置 → 输出对比报告
  - Config A：纯向量检索（baseline）
  - Config B：BM25+向量混合检索
  - Config C：混合检索 + 重排
- **报告生成**：Markdown 报告（每类准确率 + 总体得分 + 失败案例抽样分析）→ 存 `docs/eval-report-{date}.md`，**面试直接展示**

### 5.5 MCP 工具接入（P0）
- 使用官方 `mcp` Python SDK（client 模式）
- 工具流：MCP 服务端（用 FastMCP 写一个 demo 服务，如「库存查询服务」）→ 通过 MCP 协议暴露工具 → Agent 的 Tool Registry 动态注册这些工具
- 交付物：`mcp_servers/inventory_server.py`（演示用库存服务）+ 接入代码
- **面试叙事**：「我实现了 MCP 客户端，让 Agent 能通过标准协议接入任意符合 MCP 规范的外部工具，而不是把工具写死在代码里」

### 5.6 Langfuse 可观测（P0）
- 自托管 Langfuse（Docker 一条命令）或 Langfuse Cloud（免费额度）
- 埋点范围：LLM 调用、检索（查询+命中片段）、工具调用（入参出参）、Agent 轨迹
- UI 展示：会话追踪树、token 消耗、延迟分析

### 5.7 记忆 / 文档管理 / UI / 部署
- 记忆：同 Lite（滑窗 8 轮 + 20 轮触发摘要），存 Redis + PostgreSQL
- 文档管理：同 Lite，增加切片质量检查（空切片/超短切片告警）
- UI：Streamlit 增加「评测报告」页（渲染 Markdown 报告）与「追踪」入口
- 部署：Docker Compose 编排 postgres(pgvector) + redis + langfuse + api + ui

---

## 6. 数据模型（在 Lite 版基础上新增）

```sql
-- 评测用例表（新增）
CREATE TABLE eval_cases (
    id          BIGSERIAL PRIMARY KEY,
    category    TEXT NOT NULL,       -- doc_qa / cross_doc / tool_call
    question    TEXT NOT NULL,
    reference   TEXT NOT NULL,       -- 参考答案
    doc_ids     UUID[],              -- 依赖文档（可为空）
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 评测运行记录（新增）
CREATE TABLE eval_runs (
    id          BIGSERIAL PRIMARY KEY,
    config_name TEXT NOT NULL,       -- pure_vector / hybrid / hybrid_rerank
    score_correctness FLOAT,
    score_faithfulness FLOAT,
    score_completeness FLOAT,
    accuracy    FLOAT,               -- 正确率（judge 得分≥4 计对）
    report_path TEXT,                -- 生成的报告文件路径
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 工具调用记录（Lite 已有，扩展字段）
ALTER TABLE messages ADD COLUMN tool_results JSONB;
```

其余表（sessions/messages/documents/document_chunks）同 Lite 版 PRD，不重复。

---

## 7. API 列表（Lite 版基础上新增/变更）

| 方法 | 路径 | 说明 | 变更 |
|---|---|---|---|
| POST | /api/chat | 对话（SSE 流式） | 同 |
| GET | /api/sessions | 会话列表 | 同 |
| GET | /api/sessions/{id}/messages | 会话历史 | 同 |
| POST | /api/documents | 上传文档 | 同 |
| GET | /api/documents | 文档列表 | 同 |
| DELETE | /api/documents/{id} | 删除文档 | 同 |
| GET | /health | 健康检查 | 同 |
| **GET** | **/api/eval/cases** | 评测集列表/管理 | ⭐ 新增 |
| **POST** | **/api/eval/cases** | 添加评测用例 | ⭐ 新增 |
| **POST** | **/api/eval/run** | 触发评测（指定 config） | ⭐ 新增 |
| **GET** | **/api/eval/runs/{id}** | 评测结果 | ⭐ 新增 |
| **GET** | **/api/tools** | 已注册工具列表（含 MCP 工具） | ⭐ 新增 |
| **POST** | **/api/mcp/register** | 注册 MCP 服务 | ⭐ 新增 |

---

## 8. 里程碑与每日验收（35 天冲刺计划）

> 每天 3-5 小时。**每天结束必须跑通当日验收点**，否则当晚只修当前问题，不叠加新功能。

| 阶段 | 天 | 内容 | 当日验收标准 |
|---|---|---|---|
| **M0 骨架+分层** | D1 | 项目脚手架、config、健康检查 | uvicorn 启动 + /health ok |
| | D2 | **分层架构**：LLM Gateway / Tool Registry / Agent 引擎 三个独立模块目录 | 三层独立可 import，无循环依赖 |
| | D3 | ORM 模型 + 迁移 + Redis | 建表成功，/api/sessions 可用 |
| | D4 | Langfuse 接入（自托管 Docker）| 任意一次 LLM 调用出现在 Langfuse 面板 |
| **M1 引擎** | D5 | LLM Gateway 双通道 + 直答 | CLI 问「你好」正常回答，Langfuse 有记录 |
| | D6 | LangGraph ReAct + 时间/计算工具 | 「现在几点」走通 工具→回答 |
| | D7 | Tool Registry 注册制 + db_query 工具 | 「查库存表前 3 条」返回真实数据 |
| | D8 | 记忆（滑窗）+ 5 轮对话测试 | 连续 5 轮上下文正确 |
| **M2 RAG 基础** | D9 | 文档解析 + 切片 | 上传 md 文档 → chunk 入库 |
| | D10 | 向量化 + pgvector 检索 | 问文档内问题有命中片段 |
| | D11 | 生成 + 引用定位 | 答案含 [1] 引用且可定位原文 |
| | D12 | 摘要记忆（20 轮触发）| 长对话摘要生效 |
| **M3 RAG 增强** | D13 | **BM25 检索（pg tsvector）** | 关键词检索命中率正常 |
| | D14 | **RRF 融合（混合检索）** | 混合检索结果优于单路（肉眼抽样） |
| | D15 | **重排（bge-reranker）** | top20→top5 重排生效 |
| | D16 | 检索策略配置化（消融开关） | 3 种 config 可切换 |
| **M4 评测体系** | D17 | 评测集 50 条入库 + 管理 API | /api/eval/cases 可用 |
| | D18 | **LLM-as-judge 评分器** | 单条用例可自动评分 |
| | D19 | **消融实验脚本**（3 config 自动跑） | 3 个 config 各跑完 50 条 |
| | D20 | **评测报告生成**（Markdown+图表） | 报告含准确率对比 + 失败案例分析 |
| **M5 MCP+可观测** | D21 | FastMCP 库存服务 | MCP 服务端独立可调 |
| | D22 | MCP 客户端接入 Tool Registry | 对话中可调用 MCP 库存工具 |
| | D23 | 检索/工具/LLM 三层 Langfuse 埋点补全 | 一次对话全链路可在 Langfuse 查看 |
| **M6 工程化** | D24 | Docker Compose 全编排 + pytest | 全新环境一键起，核心测试通过 |
| | D25 | README/架构图/演示脚本 + 全量回归 | 5 分钟 demo 可复现 + 评测报告定稿 |

**D25 验收即阶段 1 完成**：产出「评测报告 + Langfuse 追踪演示 + MCP 工具演示」三件套，简历可写、面试可讲。

---

## 9. 岗位能力映射（西安 + 远程真实 JD）

| 岗位（真实 JD） | 本项目覆盖点 | 阶段 |
|---|---|---|
| 阿里云 Agent 开发 20-50k：效果评估体系 | ✅ 评测体系（F5）是核心交付物 | 1 |
| 阿里云：重度使用 Cursor/Claude Code/SDD | ✅ 开发全程用 AI 编程工具，简历体现 | 1 |
| 华为 2012：上下文组装/记忆管理 | ✅ 记忆模块（F7） | 1 |
| 华为 AI 应用：RAG/向量库（Milvus） | ✅ RAG 增强（pgvector） | 1 |
| 北方华鑫：检索召回率调优 | ✅ 消融实验证明调优过程 | 1 |
| 昆仑联通：Dify/RAGFlow 定制化 | ✅ 可讲「为什么自建而非用 Dify」（评测+可观测+可控） | 1 |
| 远程岗：MCP 协议 | ✅ MCP 接入（F6） | 1 |
| 海外远程：可观测性/追踪 | ✅ Langfuse（F8） | 1 |
| 多智能体协作 | ⏳ 阶段 2 | 2 |
| Agent Runtime/Harness | ⏳ 阶段 3 | 3 |

---

## 10. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 重排模型部署成本/依赖 | 中 | 中 | bge-reranker-base 本地 ONNX 或走 API；失败时降级不重排 |
| Langfuse 自托管复杂 | 中 | 中 | 先用 Langfuse Cloud 免费额度，Docker 自托管留到 D24 |
| 评测集质量影响数据可信度 | 中 | 高 | 50 条用例 3 类分布固定；judge 评分规则写死防漂移 |
| MCP SDK 版本兼容 | 中 | 中 | 锁版本；MCP 服务端用 FastMCP 最小实现 |
| 时间超支 | 高 | 高 | P1 全部可砍；每日验收红线；D16 后不再加新功能 |
| 35 天坚持问题（中后段倦怠） | 中 | 高 | 里程碑 M2 后穿插「小胜利」（消融实验首版报告）保持动力 |

---

## 11. 面试叙事脚本（阶段 1 完成后）

**30 秒电梯陈述**：
> 我做了个企业知识库 Agent 平台（AgentForge）。它不是 demo——支持文档上传、混合检索+重排的 RAG 问答、MCP 协议接入外部工具、滑窗+摘要记忆，全部走 Langfuse 可观测。最关键的是我建了一套评测体系：50 条评测集 + LLM-as-judge 自动评分，跑了消融实验证明混合检索+重排比纯向量检索准确率提升 10%+。架构上我把 LLM Gateway、工具注册表、Agent 引擎完全解耦，后续可以平滑升级到多 Agent 编排。

**面试官追问防御**：
- 问「为什么不用 Dify？」→ 自建的理由：可控性（检索链路可调）+ 可评测（Dify 黑盒）+ 可演进（分层架构）
- 问「检索不准怎么办？」→ 直接展示消融实验报告和数据
- 问「你怎么知道效果好不好？」→ 展示评测集 + LLM-as-judge + 失败案例分析
- 问「工具怎么扩展？」→ 展示 MCP 接入，讲标准协议 vs 写死代码的区别

---

*— 阶段 1 PRD 完 ｜ 下一里程碑：M0 骨架 D1 开始 —*
