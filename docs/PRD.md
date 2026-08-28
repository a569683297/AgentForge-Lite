# AgentForge 阶段 1 — 详细产品需求文档（PRD）

> **文档版本**：v3.0（详细版）
> **编制日期**：2026-08-28
> **项目状态**：已确认，待开发（D1 起）
> **演进关系**：阶段 1（RAG+ 增强版，39 天核心 + 5 天缓冲，含学习消化）→ 阶段 2（多 Agent 版，+14 天）→ 阶段 3（完整 Runtime 平台，+14 天）。本 PRD 覆盖阶段 1 全部范围。

---

## 1. 文档信息

| 项 | 内容 |
|---|---|
| 项目名称 | AgentForge（阶段 1：RAG+ 增强版） |
| 项目代码 | agentforge-stage1 |
| 项目根目录 | `/Users/779369901qq.com/workspace/bs/AgentForge-Lite/` |
| 主要语言 | Python 3.12 |
| 开发方式 | AI 辅助（Cursor/Claude Code 等）+ 人工审查，每模块先讲原理再写码 |
| 目标岗位 | 西安/远程 AI-Agent 开发岗（12-50k） |

---

## 2. 项目背景

### 2.1 需求来源
基于深圳 4 份 + 西安 8 份 + 国内/海外远程 6 份共 **18 份真实 AI-Agent 开发岗位 JD** 的能力图谱调研：

**100% 高频考点**：LangChain/LangGraph、RAG 全流程、Tool Use/Function Calling、Python 后端（FastAPI/Flask）
**高薪岗差异化考点（20-50k）**：Agent 效果评估体系（阿里云点名）、多智能体协作（阿里云/华为）、记忆管理（华为）、MCP 协议（远程岗必考）、可观测性（海外远程岗硬要求）、AI 编程工具重度使用（阿里云）

### 2.2 机会窗口
- 市场上大量求职者项目停留在"RAG 问答 demo"层（教程复刻，无评测、无观测、无工具）
- 阿里云/华为 JD 明确要"效果评估体系建设"，**会用数据驱动优化 Agent 的人极少**
- 本项目通过「评测体系 + 可观测 + MCP + 分层架构」建立差异化

### 2.3 为什么自建而非用 Dify/RAGFlow
| 维度 | Dify 等平台 | 自建（本项目） |
|---|---|---|
| 检索链路可控性 | 黑盒，不可调 | 全链路自研，可做消融实验 |
| 效果可评测 | 无内置评测体系 | 评测集 + LLM-as-judge + 消融 |
| 可演进性 | 平台限制 | 分层架构，可升多 Agent/平台 |
| 面试说服力 | "我用了 Dify" | "我实现了 xxx，数据证明 xxx" |

---

## 3. 产品定位与价值

**一句话定位**：企业知识库驱动的智能问答 Agent 平台（阶段 1 交付核心闭环）——让企业上传私有文档后，获得一个能带引用回答、能调外部工具、能记住上下文、**且效果可量化、过程可观测**的 AI 助手。

**三句价值主张**：
1. **答得准**：混合检索 + 重排，答案带可点击的原文引用
2. **有依据**：评测体系用数据证明"我的系统效果比基线好 10%+"
3. **能落地**：Docker 一键部署，全链路可观测，可对接真实业务工具（MCP）

---

## 4. 目标与成功指标（验收底线）

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

---

## 5. 用户角色与核心场景

### 5.1 角色
| 角色 | 说明 | 使用方式 |
|---|---|---|
| 终端用户 | 向 Agent 提问，获得带引用答案 | Web 对话页 |
| 知识管理员 | 上传/删除文档，查看入库状态 | Web 知识库页 |
| 系统管理员（阶段 3 完善） | 查看评测报告、追踪面板 | Web 管理页 |

### 5.2 核心场景（端到端叙事）

**场景 A：文档问答（RAG）**
> 管理员上传《公司考勤制度.pdf》→ 系统解析、切片、向量化入库（状态：processing → ready）→ 用户问"年假怎么算"→ Agent 引擎判断需要检索 → RAG 混合检索 top5 → 重排 → 组装 prompt → 模型生成答案并标注 [1][2] → UI 展示答案 + 可点击引用 → 全程进入 Langfuse 追踪

**场景 B：工具调用（Tool Use）**
> 用户问"库存表里数量小于 10 的商品"→ Agent 规划：需要查数据库 → 调用 `inventory_query` 工具（经 Tool Registry / MCP）→ 执行 SQL 返回结果 → Agent 总结为自然语言 → 展示 → 工具调用过程在追踪面板可见

**场景 C：多轮记忆（Memory）**
> 用户先问"上季度营收"→ 再问"环比呢"→ Agent 从滑窗记忆中取出"上季度营收"上下文 → 正确理解"环比"→ 回答 → 超过 20 轮后自动摘要压缩历史，摘要作为长期记忆

**场景 D：评测与优化（差异化核心）**
> 管理员点击"运行评测"→ 系统用 50 条评测集跑 3 个检索配置（纯向量/混合/混合+重排）→ LLM-as-judge 自动打分 → 生成 Markdown 报告（含准确率对比、失败案例分析）→ 管理员查看报告 → 选择最优配置作为线上默认

---

## 6. 范围

### 6.1 In Scope（阶段 1 必须交付）
1. LLM Gateway（DeepSeek + OpenAI 双通道，统一接口，Langfuse 埋点）
2. Agent 引擎（LangGraph ReAct，5 步上限，工具/检索决策）
3. RAG 引擎（解析 → 切片 → 向量化 → 混合检索 → 重排 → 引用生成）
4. 工具注册中心 + MCP 接入（FastMCP 库存服务 demo）
5. 记忆系统（滑窗 8 轮 + 20 轮摘要 + Redis/PG 持久化）
6. 评测体系（50 条评测集 + LLM-as-judge + 消融实验 + 报告）
7. Langfuse 可观测（LLM/检索/工具三层埋点）
8. Web UI（对话页 + 知识库管理 + 引用高亮 + 评测报告页）
9. 部署（Docker Compose：pgvector + redis + langfuse + api + ui）
10. 测试（pytest 核心链路）+ 文档（README/架构图/演示脚本）

### 6.2 Out of Scope（阶段 2/3 或明确不做）
| 项 | 归属 |
|---|---|
| 多 Agent 编排（Supervisor/Planner-Executor） | 阶段 2 |
| Agent Runtime 平台化（状态持久化/多通道 Gateway/消息总线） | 阶段 3 |
| 管理后台完整版、可视化工作流画布 | 阶段 3 |
| 模型微调、知识图谱 | 明确不做 |
| 用户体系/计费/权限矩阵 | 明确不做（阶段 1 单用户） |

---

## 7. 功能需求详情

### F1 对话接口（P0）
- **F1.1** POST `/api/chat`：入参 `{session_id?, message, stream?}`，出参 SSE 流
- **F1.2** 支持流式（SSE）与非流式两种模式
- **F1.3** 会话不存在时自动创建，返回 session_id
- **F1.4** 每轮对话落库（messages 表），更新 session.updated_at
- **验收**：curl 发 3 轮对话，上下文正确；SSE 逐字返回

### F2 Agent 引擎（P0）
- **F2.1** LangGraph StateGraph：START → plan → (工具/检索 or 直接) → observe → ... → answer → END
- **F2.2** 状态：`messages[] + step_count + tool_results[]`，step 上限 5
- **F2.3** plan 节点注入工具 schema（JSON Schema），LLM 决定调用哪个工具
- **F2.4** 无工具调用时直接走 answer；工具异常时把错误信息返回给 LLM 重试 1 次
- **F2.5** 检索决策：LLM 判断"是否需要检索知识库"（可用"是否命中检索触发词或问题涉及知识"）
- **验收**：CLI 脚本分别测"直答 / 调工具 / 检索"三条路径

### F3 RAG 引擎（P0）
- **F3.1** 文档解析：PDF（pypdf）/ DOCX（python-docx）/ MD / TXT，按类型分发
- **F3.2** 切片：512 token + 10% 重叠；中文按字符近似；保留元信息（页码/标题路径）
- **F3.3** 向量化：embedding 模型经 LLM Gateway 统一调用，pgvector 存储
- **F3.4** 混合检索：BM25（PostgreSQL tsvector）+ 向量（cosine）→ **RRF 融合**（k=60）
- **F3.5** 重排：bge-reranker-base（本地 ONNX 或 API），top20 → top5
- **F3.6** 检索策略可配置（`pure_vector` / `hybrid` / `hybrid_rerank`），供评测复用
- **F3.7** 引用定位：返回片段带 document_id + 页码/标题，答案生成强制标注 [n]
- **验收**：上传 md 文档，问 10 个问题，≥8 个引用可定位到原文

### F4 文档管理（P0）
- **F4.1** POST `/api/documents`（multipart），入库状态 processing → ready/failed
- **F4.2** GET / DELETE `/api/documents[/{id}]`
- **F4.3** 切片质量检查：空切片/超短切片告警（日志 + UI 状态）
- **验收**：上传/列表/删除全通；失败文档（坏 PDF）有 failed 状态

### F5 记忆系统（P0）
- **F5.1** 滑窗：最近 8 轮（user+assistant），超窗先摘要再丢弃
- **F5.2** 摘要：会话累计 >20 轮时，LLM 生成历史摘要，作为 system 前缀
- **F5.3** 存储：消息全量 PostgreSQL，热窗口 Redis（session_id 为 key，TTL 24h）
- **验收**：连续 5 轮上下文正确；模拟 21 轮后摘要生效且不丢失关键事实

### F6 工具注册中心 + MCP（P0）
- **F6.1** ToolRegistry：`name/description/parameters(schema)/handler` 四元组注册制
- **F6.2** 内置工具：`current_time`（时间/计算）、`web_search`（可选，模拟或真实 API）
- **F6.3** MCP 客户端：官方 `mcp` Python SDK，连接 FastMCP 服务端
- **F6.4** MCP demo 服务：`mcp_servers/inventory_server.py`——库存查询服务（内存 mock 数据），暴露 `query_inventory(filters)` 工具
- **F6.5** 工具调用失败不影响主流程（错误返回给 LLM 重试或最终兜底回答）
- **验收**：对话中问"查库存数量小于 10 的商品"→ 走通 MCP 工具 → 返回真实 mock 数据

### F7 评测体系（P0，差异化核心）
- **F7.1** 评测集：50 条，3 类
  - 类型 A 文档问答 30 条（含 5 条"文档中无答案"负例，测幻觉控制）
  - 类型 B 跨文档推理 10 条
  - 类型 C 工具调用 10 条
- **F7.2** LLM-as-judge 评分器：judge 模型按 3 维度打分（0-5）：
  - 正确性（Correctness）：与参考答案匹配
  - 引用忠实度（Faithfulness）：内容是否都有引用支撑
  - 完整性（Completeness）：是否遗漏关键信息
- **F7.3** 消融实验：同一评测集跑 3 配置（pure_vector / hybrid / hybrid_rerank），自动输出对比
- **F7.4** 报告生成：Markdown（每类准确率 + 总体得分 + 失败案例抽样分析）→ `docs/eval-report-{date}.md`
- **F7.5** 准确率口径：judge 得分 ≥4/5 计为"对"
- **验收**：一键跑完 3×50 条；报告含对比数据与失败案例

### F8 Langfuse 可观测（P0）
- **F8.1** 部署：Langfuse Cloud（免费额度）或 Docker 自托管
- **F8.2** 埋点：LLM 调用（model/token/耗时）、检索（query/命中片段）、工具（入参出参）、Agent 轨迹（trace/span）
- **F8.3** UI 展示：会话追踪树、token 消耗、延迟分析
- **验收**：一次"带工具调用的对话"在面板中完整可见全链路

### F9 Web UI（P0，React + TS + Vite）
- **F9.1** 对话页（ChatPage）：消息流、SSE 流式展示（打字机效果）、引用高亮（Citation 组件点击展开原文）、新建会话
- **F9.2** 知识库页（KnowledgePage）：上传、进度状态、文档列表、删除
- **F9.3** 评测页（EvalPage）：触发评测、轮询进度、渲染 Markdown 报告
- **F9.4** 侧边栏：模型选择（deepseek/openai）、会话历史
- **F9.5** 技术要点：axios + fetch 处理 SSE；vite dev proxy → :8000；Ant Design 组件库
- **验收**：浏览器完整走通 场景 A/B/C/D；打字机流式 + 引用高亮可用

### F10 部署与测试（P0）
- **F10.1** docker-compose.yml：pgvector + redis + langfuse + api + ui
- **F10.2** pytest：核心链路（LLM 直答/工具/RAG 检索/记忆/评测 API）
- **F10.3** README（架构图 + 启动指南 + 演示脚本）
- **验收**：全新环境 docker compose up 后 10 分钟可演示

### P1（有余力）
- P1-1 JWT 简单鉴权
- P1-2 管理页雏形（模型/工具配置）
- P1-3 更多 MCP 工具
- P1-4 Redis 相似问题缓存

---

## 8. 技术架构

### 8.1 总体架构图

```
┌───────────────────────────────────────────────────────────────┐
│                    Web UI（React + TS + Vite）                  │
│   对话页 ｜ 知识库管理 ｜ 引用高亮 ｜ 评测报告 ｜ 模型选择          │
└──────────────────────────┬────────────────────────────────────┘
                           │ HTTP/SSE
┌──────────────────────────▼────────────────────────────────────┐
│                  API 层（FastAPI + Pydantic v2）                │
│   /chat /documents /sessions /eval /tools /health               │
└──────────────────────────┬────────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────────┐
│              Agent 引擎（LangGraph ReAct）★独立服务★            │
│   plan → [tool?/rag?] → execute → observe → ... → answer       │
│        │                 │                                     │
│        │                 ▼                                     │
│        │      ┌──────────────────────┐                         │
│        │      │  Tool Registry ★     │ ← MCP Client → 外部服务   │
│        │      │  current_time        │                          │
│        │      │  inventory(MCP)      │                          │
│        │      └──────────────────────┘                         │
│        │                                                      │
│        ▼                                                      │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐    │
│  │ RAG Engine ★ │  │ Memory Service│  │ Eval Service ★   │    │
│  │ 混合检索+重排  │  │ 滑窗+摘要      │  │ 评测集/评分/消融  │    │
│  └──────────────┘  └───────────────┘  └──────────────────┘    │
│        │                 │                 │                   │
│        ▼                 ▼                 ▼                   │
│  ┌───────────────────────────────────────────────────────┐    │
│  │        LLM Gateway ★（DeepSeek/OpenAI 双通道）          │    │
│  │        统一 chat/embed 接口 + Langfuse 埋点            │    │
│  └───────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────┘
        │               │               │
        ▼               ▼               ▼
   PostgreSQL        Redis         Langfuse
   (pgvector+tsvector)(会话缓存)    (可观测)
```

### 8.2 分层架构与扩展点（阶段 2/3 零返工前提）

| 层 | 职责 | 阶段 1 实现 | 阶段 2/3 复用 |
|---|---|---|---|
| **LLM Gateway** | 所有模型调用唯一入口 | DeepSeek/OpenAI 双通道 + 埋点 | 阶段 3 加多 provider/降级/计费 |
| **Tool Registry** | 工具注册制管理 | 内置 1 + MCP 1 | 阶段 2 多 Agent 挂不同工具集 |
| **Agent 引擎** | LangGraph 图执行 | 单 Agent ReAct | 阶段 2 嵌为子图 |
| **RAG Engine** | 检索链路 | 混合+重排+配置化 | 不变，多 Agent 共用 |
| **Eval Service** | 评测 | 评测集+judge+消融 | 阶段 2 评测多 Agent 轨迹 |
| **可观测** | 追踪 | Langfuse 三层埋点 | 阶段 3 管理后台消费数据 |

**红线**：业务代码不得绕过 Gateway 直连 SDK；Agent 不得直接调函数（必须经 Registry）；评测模块与业务解耦（输入配置+评测集，输出报告）。

### 8.3 目录结构（目标）

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
│   │   └── eval.py
│   ├── schemas/                # Pydantic 模型
│   │   ├── chat.py
│   │   ├── document.py
│   │   ├── eval.py
│   │   └── common.py
│   ├── services/
│   │   ├── llm_gateway.py      # ★ LLM 统一入口 + 埋点
│   │   ├── agent_service.py    # ★ LangGraph 编排
│   │   ├── rag_service.py      # ★ 混合检索+重排
│   │   ├── memory_service.py   # 滑窗+摘要
│   │   ├── eval_service.py     # ★ 评测/消融/报告
│   │   └── document_service.py # 解析/切片/入库
│   ├── tools/
│   │   ├── registry.py         # ★ 工具注册中心
│   │   ├── current_time.py
│   │   ├── mcp_client.py       # ★ MCP 客户端
│   │   └── inventory.py        # MCP 库存工具包装
│   └── prompts/                # 所有 prompt 集中管理
│       ├── system.py
│       ├── rag.py
│       ├── summarizer.py
│       └── judge.py
├── mcp_servers/
│   └── inventory_server.py     # FastMCP 库存服务（demo）
├── tests/
│   ├── test_chat.py
│   ├── test_agent.py
│   ├── test_rag.py
│   ├── test_memory.py
│   ├── test_eval.py
│   └── conftest.py
├── frontend/                    # React + TS + Vite（用户主战场）
│   ├── src/
│   │   ├── App.tsx
│   │   ├── api/
│   │   │   ├── client.ts        # axios 封装（SSE 处理）
│   │   │   ├── chat.ts
│   │   │   ├── documents.ts
│   │   │   └── eval.ts
│   │   ├── pages/
│   │   │   ├── ChatPage.tsx     # 对话页（流式展示/引用高亮）
│   │   │   ├── KnowledgePage.tsx# 知识库管理
│   │   │   └── EvalPage.tsx     # 评测报告
│   │   ├── components/
│   │   │   ├── MessageList.tsx
│   │   │   ├── Citation.tsx     # 引用高亮组件
│   │   │   └── ChatInput.tsx
│   │   └── styles/
│   ├── package.json
│   ├── vite.config.ts           # dev proxy → :8000
│   └── tsconfig.json
├── scripts/
│   ├── eval_run.py             # 命令行跑评测
│   └── seed_demo_data.py       # 示例文档+评测集
├── data/
│   └── sample_docs/            # 演示文档
├── docs/
│   ├── PRD.md                  # 本文件
│   ├── eval-report-{date}.md   # 评测报告
│   └── architecture.md
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

### 8.4 技术选型与理由

| 技术 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | JD 主流；uv 管理（已装 3.12.13） |
| Web 框架 | FastAPI | 异步、Pydantic v2 集成、自动 OpenAPI |
| Agent | LangGraph | 状态机图、checkpoint、stream、可演进 |
| LLM 接入 | DeepSeek + OpenAI | 双通道降级；JD 高频 |
| 向量库 | pgvector | 少一个中间件，Docker 一条命令 |
| BM25 | PostgreSQL tsvector | 免 ES 重量级组件，够用 |
| 重排 | bge-reranker-base | 中文效果好，可本地/API |
| 缓存 | Redis | 会话热窗口、后续缓存 |
| 可观测 | Langfuse | 海外远程岗点名；开源自托管 |
| MCP | 官方 mcp SDK + FastMCP | 标准协议，JD 必考 |
| UI | React + TS + Vite + Ant Design | 用户主战场，全栈真实感；JD 要求前端能力 |
| 测试 | pytest + pytest-asyncio | 标准 |
| 部署 | Docker Compose | 一键演示 |

---

## 9. 模块详细设计

### 9.1 LLM Gateway

```
class LLMClient:
    async def chat(messages, model=None, stream=False, temperature=None) -> str | AsyncIterator[str]
    async def embed(texts: list[str]) -> list[list[float]]
```

- **通道配置**（.env）：`LLM_PROVIDER=deepseek|openai`，`DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
- **双通道**：主通道失败（超时/4xx/5xx）→ 备用通道重试 1 次
- **场景参数**：规划 temp=0.2、回答 temp=0.4、摘要 temp=0.1、评测 judge temp=0
- **Langfuse 埋点**：每个 span 记录 model/input/output/token/耗时
- **embed 统一**：RAG 与评测都走 gateway.embed，避免散落

**关键设计决策**：所有 LLM 调用必须经 gateway——这是阶段 3 加多 provider 不返工的前提。

### 9.2 Agent 引擎（LangGraph）

```
class AgentState(TypedDict):
    messages: list[dict]        # 完整对话（含工具消息）
    step_count: int
    tool_results: list[dict]

graph = StateGraph(AgentState)
graph.add_node("plan", plan_node)          # LLM 决策：工具/检索/直答
graph.add_node("execute", execute_node)    # 执行工具 or 检索
graph.add_node("observe", observe_node)    # 结果写回 messages
graph.add_node("answer", answer_node)      # 最终回答
graph.add_edge("plan", "execute")
graph.add_edge("execute", "observe")
graph.add_edge("observe", "plan")
graph.add_conditional_edges("plan", should_answer, {"answer": "answer"})
graph.add_edge("answer", END)
```

- `should_answer`：plan 输出无工具调用且步骤够 → answer
- `recursion_limit` = 10（plan 最多跑 5 次 → 5 步）
- 工具消息格式：`{"role": "tool", "tool_call_id": ..., "content": ...}`

### 9.3 RAG 引擎

**检索流程**：
```
query → 向量检索（pgvector cosine top20）
      → BM25 检索（pg tsvector top20）
      → RRF 融合（k=60）→ top20
      → bge-reranker 重排 → top5
      → 组装 prompt（片段+引用编号）
```

**RRF 公式**：`score(d) = Σ 1/(k + rank_i(d))`，k=60

**切片策略**：512 token + 10% 重叠；中文按字符（~750 字符）近似；保留 `page_ref` 与标题路径用于引用定位。

**检索策略配置**（config）：`RETRIEVER_CONFIG = pure_vector | hybrid | hybrid_rerank`

### 9.4 记忆系统

- 热窗口：Redis `session:{id}:window`，存最近 8 轮
- 冷数据：PostgreSQL messages 全量
- 摘要触发：轮数 >20 → `summarizer` prompt 生成摘要 → 存 session.summary → 作为 system 前缀
- 组装顺序：`system(人设+摘要) → 窗口内消息`

### 9.5 评测体系

**评测集格式**：
```json
{
  "id": 1, "category": "doc_qa|cross_doc|tool_call",
  "question": "...", "reference": "参考答案",
  "doc_ids": ["uuid"], "expected_tool": "inventory_query"
}
```

**Judge 评分**（judge prompt）：
```
你是评测员。对照参考答案和文档依据，按以下维度给 0-5 分：
1. 正确性：答案是否与参考答案一致
2. 引用忠实度：每个论断是否有引用/工具结果支撑
3. 完整性：是否遗漏关键信息
输出 JSON: {"correctness": n, "faithfulness": n, "completeness": n}
```

**消融实验矩阵**：

| 配置 | 检索 | 重排 |
|---|---|---|
| A. pure_vector | 仅向量 top5 | 无 |
| B. hybrid | BM25+向量 RRF top5 | 无 |
| C. hybrid_rerank | BM25+向量 RRF top20 | bge-reranker top5 |

**报告结构**：总体对比表（3 配置 × 3 维度 × 准确率）→ 每类明细 → 失败案例（各取 3 条）→ 结论与建议。

### 9.6 MCP 接入

**mcp_servers/inventory_server.py**（FastMCP）：
```python
from fastmcp import FastMCP
mcp = FastMCP("inventory")
@mcp.tool()
def query_inventory(min_stock: int | None = None, category: str | None = None) -> list[dict]:
    """查询库存商品。min_stock: 库存下限过滤; category: 品类过滤"""
    # mock 数据：返回商品列表
```
- 运行：`python mcp_servers/inventory_server.py`（stdio 或 SSE）
- 客户端：`mcp_client.py` 连接 → 动态发现工具 → 注册进 ToolRegistry

### 9.7 Web UI（React + TS + Vite）

- **脚手架**：Vite + React + TypeScript + Ant Design
- **页面**：`ChatPage`（对话/流式/引用）、`KnowledgePage`（知识库管理）、`EvalPage`（评测报告）
- **SSE 流式**：fetch + ReadableStream 逐块解析，打字机效果展示
- **引用高亮**：`Citation` 组件，点击 `[n]` 弹出原文片段（doc_title + page_ref）
- **API 对接**：axios 封装，`vite.config.ts` 配置 dev proxy 转发到 `:8000`
- **生产构建**：`npm run build` → 产物由 FastAPI 静态托管或 Nginx

### 9.8 部署

**docker-compose.yml 服务**：
| 服务 | 镜像 | 端口 |
|---|---|---|
| db | pgvector/pgvector:pg16 | 5432 |
| redis | redis:7-alpine | 6379 |
| langfuse | langfuse/langfuse:latest | 3000 |
| api | 本地构建 | 8000 |
| web | node:22 构建 + nginx 托管前端 | 80 |
| ui | 本地构建 | 8501 |

**health 检查**：`GET /health` 返回各依赖连接状态。

---

## 10. 数据模型

```sql
-- 会话
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL DEFAULT '新会话',
    summary TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 消息
CREATE TABLE messages (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,               -- user/assistant/tool
    content TEXT NOT NULL,
    tool_calls JSONB,
    tool_results JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_messages_session ON messages(session_id, created_at);

-- 文档
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,          -- pdf/docx/md/txt
    status TEXT NOT NULL DEFAULT 'processing',  -- processing/ready/failed
    chunk_count INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 切片（含向量）
CREATE TABLE document_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    page_ref TEXT,
    title_path TEXT,
    embedding vector(1536)
);
CREATE INDEX idx_chunks_embedding ON document_chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_chunks_doc ON document_chunks(document_id);

-- 全文检索（BM25 用）
ALTER TABLE document_chunks ADD COLUMN content_tsv tsvector;
CREATE INDEX idx_chunks_tsv ON document_chunks USING gin(content_tsv);

-- 评测用例
CREATE TABLE eval_cases (
    id BIGSERIAL PRIMARY KEY,
    category TEXT NOT NULL,           -- doc_qa/cross_doc/tool_call
    question TEXT NOT NULL,
    reference TEXT NOT NULL,
    doc_ids UUID[],
    expected_tool TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 评测运行
CREATE TABLE eval_runs (
    id BIGSERIAL PRIMARY KEY,
    config_name TEXT NOT NULL,        -- pure_vector/hybrid/hybrid_rerank
    score_correctness FLOAT,
    score_faithfulness FLOAT,
    score_completeness FLOAT,
    accuracy FLOAT,
    report_path TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## 11. API 设计

| 方法 | 路径 | 说明 | 请求 | 响应 |
|---|---|---|---|---|
| POST | /api/chat | 对话（流式/非流式） | `{session_id?, message, stream?}` | SSE 或 `{session_id, reply}` |
| GET | /api/sessions | 会话列表 | - | `[{id,title,updated_at}]` |
| GET | /api/sessions/{id}/messages | 会话历史 | - | `[{role,content}]` |
| POST | /api/documents | 上传文档 | multipart | `{id,status}` |
| GET | /api/documents | 文档列表 | - | `[{id,filename,status,chunk_count}]` |
| DELETE | /api/documents/{id} | 删除（级联切片） | - | 204 |
| GET | /api/tools | 已注册工具 | - | `[{name,description}]` |
| POST | /api/mcp/register | 注册 MCP 服务 | `{command,args}` | `{tools:[...]}` |
| GET | /api/eval/cases | 评测集 | - | `[{id,category,question}]` |
| POST | /api/eval/cases | 添加用例 | `{category,question,reference}` | `{id}` |
| POST | /api/eval/run | 触发评测 | `{config?}` | `{run_id}` |
| GET | /api/eval/runs/{id} | 评测结果 | - | `{config,accuracy,report_path}` |
| GET | /health | 健康检查 | - | `{status:"ok",deps:{...}}` |

**chat 响应示例（非流式）**：
```json
{
  "session_id": "6f2c...",
  "reply": "根据《公司考勤制度》第 3 章，年假按工龄计算：满 1 年 5 天，满 3 年 10 天[1]。",
  "citations": [{"doc_id": "...", "page_ref": "p.12", "snippet": "年假：工龄满1年5天..."}]
}
```

---

## 12. 非功能性需求

| 类别 | 要求 |
|---|---|
| 性能 | 单轮 P95 < 5s（含 RAG）；SSE 首 token < 1.5s |
| 可用性 | docker compose up 10 分钟可演示；所有依赖有 health 检查 |
| 安全 | API key 仅存 .env（gitignore）；.env.example 脱敏；日志不打印 key |
| 可观测 | 100% 对话有 Langfuse trace；错误有结构化日志 |
| 可维护 | 三层解耦；prompt 集中管理；核心链路有测试 |
| 可扩展 | 检索策略配置化；工具注册制；LLM 通道可加 |

---

## 13. 里程碑计划（39 天核心 + 5 天缓冲，每天 3-5h 总预算）

### 13.1 每日时间模型（已确认：3-5h = 学习+编码+验证总预算）

| 每日 3-5h 分配 | 时长 | 内容 |
|---|---|---|
| ① 学原理 | 1-1.5h | 阅读讲解文档 → 我讲端到端流程 → 你复述确认 |
| ② 编码 | 1.5-2h | 增量编码（你写 + 我辅助），每步小验证 |
| ③ 验证复盘 | 0.5-1h | 跑当日验收点、排查问题、写笔记 |

**学习占比高的天**（需预留更多理解时间，不硬赶）：D6 LangGraph（~60%）、D13-15 检索原理（~50%）、D21-24 评测方法论（~50%）、D28 MCP（~60%）。

### 13.2 里程碑（39 天核心）

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
| | D10 | 摘要记忆 + sessions API | 21 轮摘要生效 |
| **M2 RAG 基础** | D11 | 学 RAG 全流程原理 + 文档解析切片 | 能复述 RAG 端到端链路 |
| | D12 | 向量化 + pgvector 检索 | 问文档问题有命中 |
| | D13 | 生成 + 引用定位 | 答案带 [1] 且可定位 |
| | D14 | 文档管理 API 全通 | 上传/列表/删除 ✅ |
| **M3 RAG 增强** | D15 | 学 BM25 + tsvector 原理 | 能讲清 BM25 vs 向量差异 |
| | D16 | BM25 检索实现 | 关键词命中正常 |
| | D17 | RRF 混合检索 | 优于单路（抽样） |
| | D18 | 学重排原理 + bge-reranker | 能讲清重排价值 |
| | D19 | 重排 + 检索配置化 | 3 config 可切 |
| **M4 评测** | D20 | 学评测方法论（judge/消融） | 能讲清评测设计 |
| | D21 | 评测集 50 条 + API | /api/eval/cases ✅ |
| | D22 | LLM-as-judge 评分器 | 单条可评分 |
| | D23 | 消融脚本 3×50 自动跑 | 3 配置全跑通 |
| | D24 | 报告生成 | 对比表+失败案例 |
| **M5 MCP+观测** | D25 | 学 MCP 协议（重点学习日） | 能讲清 MCP 价值 |
| | D26 | FastMCP 库存服务 | 服务独立可调 |
| | D27 | MCP 客户端接入 | 对话可调库存工具 |
| | D28 | 三层埋点补全 | 全链路可追踪 |
| **M6 UI（React）** | D29 | React 脚手架（Vite+TS+AntD）+ API client | dev server 起，/health 打通 |
| | D30 | ChatPage：消息流 + SSE 打字机 + 引用高亮 | 场景 A/B/C 浏览器可演示 |
| | D31 | KnowledgePage + EvalPage + 会话侧栏 | 场景 D 可触发评测看报告 |
| **M7 工程化** | D32 | Docker Compose（+web 容器）+ pytest | 一键起，测试过 |
| | D33 | README/架构图/演示脚本 | 5 分钟 demo 可复现 |
| | D34 | 面试三件套验收（评测报告/追踪/MCP） | 全达标 |
| **M8 缓冲** | D35-39 | 缓冲（超支消化，最多 5 天） | 不推新功能 |
| | | **总周期上限 44 天（含缓冲）** | |

**红线**：
1. 每天 3-5h 是总预算，学习消化时间计入，不硬赶
2. 当天验收不过 → 只修当天，不推新
3. D19 后冻结后端新功能（P1 全部砍掉，保底 P0）
4. UI 复用用户 React 经验，不安排"学前端"时间，只排"对接+打磨"
5. 缓冲天只用于补进度，不用于加需求

---

## 14. 测试策略

| 层级 | 覆盖 | 工具 |
|---|---|---|
| 单元 | LLM Gateway 双通道/降级、RRF 融合、切片器 | pytest |
| 集成 | Agent 三路径（直答/工具/RAG）、记忆摘要、评测 API | pytest-asyncio |
| E2E | UI 走通 场景 A/B/C/D | 手工脚本 |
| 评测 | 50 条评测集 × 3 配置 | eval 脚本 |

**mock 策略**：LLM 层用 `MockLLM`（确定性回复）测逻辑；真实模型用于最终评测。

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

---

## 16. 面试叙事与演示脚本

### 16.1 30 秒电梯陈述
> 我做了个企业 Agent 平台 AgentForge。它不是 demo：文档上传 → 混合检索+重排的 RAG 问答 → MCP 协议接外部工具 → 滑窗+摘要记忆 → 全链路 Langfuse 可观测。最关键是评测体系：50 条评测集 + LLM-as-judge 打分 + 消融实验，数据证明混合+重排比纯向量准确率提升 10%+。架构上 LLM Gateway、工具注册表、Agent 引擎完全解耦，可平滑升多 Agent。

### 16.2 5 分钟演示脚本
1. **开场**（30s）：架构图讲分层
2. **知识库**（1min）：上传示例文档 → 状态 ready
3. **RAG 问答**（1.5min）：问文档问题 → 答案带引用 → 点引用看原文
4. **工具调用**（1min）：问库存 → MCP 工具执行 → 看追踪
5. **评测**（1min）：跑消融 → 展示准确率对比报告

### 16.3 面试官追问防御
| 追问 | 回答 |
|---|---|
| 为什么不用 Dify？ | 可控性（检索链路可调）+ 可评测（Dify 黑盒）+ 可演进（分层架构） |
| 检索不准怎么办？ | 展示消融实验：混合+重排提升 X%，失败案例已分析 |
| 怎么证明效果？ | 评测集 + LLM-as-judge + 失败案例分析 |
| 工具怎么扩展？ | MCP 标准协议，不写死函数 |
| 多 Agent 怎么做？ | 架构已预留：LangGraph 图嵌子图 + 工具集隔离（阶段 2 roadmap） |
| 并发性能？ | 当前单体 + Redis 缓存；阶段 3 平台化加队列/多通道 |

---

## 17. 附录

### 17.1 目标 JD 映射（18 份真实岗位）
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
| 多智能体协作（阿里云/华为） | - | 2 |
| Agent Runtime/Harness（JD3） | - | 3 |

### 17.2 示例演示文档清单
- `data/sample_docs/公司考勤制度.md`（含年假/加班/请假规则）
- `data/sample_docs/产品手册.pdf`（产品规格）
- `data/sample_docs/FAQ-常见问题.md`

---

*— 详细 PRD 完 ｜ 下一里程碑：D1 搭骨架 —*
