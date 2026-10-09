# AgentForge-Lite

> **企业级 Agent 运行时平台** —— 把「文档知识库 → 检索增强问答 → 工具调用 → 效果评测」四条链路收进一个可自托管的进程。

企业内部往往已经积累了大量制度、手册、FAQ 文档，但员工检索靠关键词翻页、问答靠人工转述。
本项目把这批文档统一入库、切分、向量化，让员工用自然语言提问并获得**带出处的回答**；
回答覆盖不到的问题，由 Agent 调用外部系统工具继续求解；每一次回答的效果都**可被量化评测**。

技术栈：FastAPI · Pydantic v2 · LangGraph · PostgreSQL + pgvector · Redis · Langfuse · React + TypeScript + Ant Design · Docker Compose

---

## 目录

- [1. 核心能力](#1-核心能力)
- [2. 架构](#2-架构)
- [3. 快速开始](#3-快速开始)
- [4. 演示](#4-演示)
- [5. MCP 接入与凭据配置](#5-mcp-接入与凭据配置)
- [6. 测试与回归](#6-测试与回归)
- [7. 目录结构](#7-目录结构)
- [8. 已知边界](#8-已知边界)
- [9. 文档索引](#9-文档索引)

---

## 1. 核心能力

| 能力 | 说明 |
|---|---|
| **文档知识库** | 支持 PDF / DOCX / Markdown / TXT 入库，自动解析、切分、向量化；PDF 保留页码，引用可定位到「第几页」 |
| **混合检索问答** | 向量检索与关键词检索双路并行 → RRF 融合 → 交叉编码器重排；返回带原文引用的回答 |
| **工具调用 Agent** | 基于状态图的推理—行动循环，可装载内置工具与外部 MCP 协议工具 |
| **会话与记忆** | 多会话管理，长对话自动摘要压缩，避免上下文溢出 |
| **评测与可观测** | 内置评测集与自动评分（多数投票）；全链路埋点可回流 Langfuse 追踪 |

> **名词速记**：**RAG**（检索增强生成）= 先检索相关文档片段、再让模型基于片段作答，以抑制幻觉；
> **RRF**（倒数排名融合）= 把多路检索结果按「排名倒数之和」合并的经典方法；
> **reranker**（重排器）= 对初筛候选逐条精算相关性的模型，比向量相似度准但更慢；
> **MCP**（Model Context Protocol）= 让模型接入外部工具／数据源的开放协议；**pgvector** = PostgreSQL 的向量扩展。

## 2. 架构

```
┌──────────────────────────────────────────────────────────────┐
│  ① 前端层     React + TypeScript + Ant Design（7 个页面）      │
│              nginx 同源转发 /api → api 容器（无 CORS 问题）    │
├──────────────────────────────────────────────────────────────┤
│  ② 接口层     FastAPI  /api/{health,chat,documents,sessions,  │
│                        eval,tools}                            │
├──────────────────────────────────────────────────────────────┤
│  ③ 服务层     检索（向量 + 关键词 + RRF + 重排）· 文档解析与切片 │
│              LLM 网关（多渠道故障转移）· 评测 · 观测 · 记忆     │
├──────────────────────────────────────────────────────────────┤
│  ④ 编排层     LangGraph 状态图：generate ⇄ execute 工具循环     │
│              工具注册表（内置工具 + MCP 动态工具，来源隔离）      │
├──────────────────────────────────────────────────────────────┤
│  ⑤ 数据层     PostgreSQL 16 + pgvector（文档/切片/会话/评测）    │
│              Redis（会话缓存）· 本地 ONNX 模型（嵌入 + 重排）    │
└──────────────────────────────────────────────────────────────┘
```

**完整的五层职责边界、一次问答的端到端数据流（⓪→⑨）、目录对照表、架构红线与偏差登记，
见 [`docs/架构图.md`](docs/架构图.md)** —— 那是本项目的架构唯一权威文档。

## 3. 快速开始

### 3.1 前置要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Docker Engine | 24.0+ | 跑四个容器 |
| Docker Compose | v2 | `docker compose` 子命令形式 |
| `uv` | 最新 | 跑 `scripts/` 下的 Python 脚本（`make warmup` / `make seed` / `make test`） |
| Node.js | 22+ | **仅宿主机**需要：`npx` 拉起外部 MCP 工具时用（容器内不需要） |

磁盘建议预留 **5 GB**（镜像 + 本地模型）；内存建议 **8 GB**。

### 3.2 三步启动

```bash
# ① 配置环境变量（模型密钥等；详见 §5.1 的必填／可选分级）
cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY=sk-xxxxxxxx

# ② 一键起容器：db / redis / api / web 四个服务
make up

# ③ 演示前初始化：播种示例文档 + 预热外部工具
make seed      # 上传 3 份示例文档（已就绪则自动跳过）
make warmup    # 预热外部 MCP 工具（首次要下载命令行包，分钟级）
```

> **为什么 `make warmup` 必须在演示前单独跑一次**：
> 外部工具由 `npx` 拉起，**首次启动要现下载命令行包**（实测直连 npm 官方源超时，走国内镜像约 2 分 31 秒）。
> 若等到演示时才触发，这 2 分半会卡在**演示第 4 段「外部系统互操作」**上，当场翻车。
> 本项目 api 容器**不装 Node**，所以这一步只能在宿主机跑 —— 它**不在** `make up` 的启动链里，
> 这一点与 PRD §9.8 的「串入 compose 启动脚本」是有意偏差（详见 §8）。
>
> **没跑 `make warmup` 也不会起不来**：连不上外部工具时 Agent 自动降级为纯知识库问答，不阻断主流程。

### 3.3 访问与冒烟验证

| 地址 | 内容 |
|---|---|
| <http://localhost:8080> | **前端界面**（从这里开始） |
| <http://localhost:18000/api/health> | 后端健康检查 |
| <http://localhost:18000/docs> | 接口文档（Swagger） |

```bash
# 三个冒烟检查
curl -s localhost:18000/api/health                 # 期望 {"status":"ok",...}
curl -s localhost:18000/api/documents | head -c 200  # 期望看到文档列表
curl -s -o /dev/null -w "%{http_code}\n" localhost:8080/   # 期望 200（前端经 nginx）
```

### 3.4 常见问题

| 现象 | 原因与处理 |
|---|---|
| 8080 / 18000 端口被占 | 本机已跑着别的东西。改 `docker-compose.yml` 的 `ports` 左侧宿主机端口 |
| 页面能开、提问报错 | `.env` 里 `DEEPSEEK_API_KEY` 没填或无效 → 检查后 `make restart` |
| 首次提问特别慢 | 没跑 `make warmup`（外部工具）或本地模型首次加载（约 1.1 GB，一次性） |
| `make up` 后 api 一直 unhealthy | `make logs` 看 api 日志；常见是 `.env` 缺失导致数据库连不上 |

---

## 4. 演示

### 4.1 一键演示脚本

```bash
scripts/demo.sh            # 按 PRD §16.2 的七段顺序逐段演示，每段打印预期输出
scripts/demo.sh --dry-run  # 只做前置检查，不发起真实提问
```

脚本的每一段都对应 PRD §16.2「5 分钟演示脚本」的一个场景，**每段自带预期输出**，
便于当场对照；任何一段失败都会明确指出断在哪里、以及是「坏了」还是「前置没满足」。

脚本开头会打印一张**段落状态表**，如实标注每一段当前是「可演 / 有条件 / 尚未实现」——
PRD §16.2 共 7 段（第 7 段本就是 v4.1 可选加段），其中第 5 段（Agentic BI 多跳下钻）依赖
指标语义层，属于尚未完成的里程碑，**脚本会明确标为未实现，而不是含糊地跳过**。

### 4.2 演示前检查清单

对应 PRD §17.3，本项目可自动化的部分已收进 `Makefile`：

```bash
make ps          # 四个容器是否都 healthy
make warmup-dry  # MCP 缓存与配置是否就绪（秒级，不起进程）
make seed        # 示例文档是否已在库（幂等，可直接复查）
curl -s localhost:18000/api/tools    # 工具装载情况
```

### 4.3 ★ 两个后端实例：工具数不一样

本项目**同时可能存在两个后端**，它们的工具装载数**不同** —— 演示时选错会「功能看起来没做」：

| 入口 | 进程 | `/api/tools` 工具总数 | 说明 |
|---|---|---|---|
| **8080 / 18000** | 容器 `api` | **3**（内置 2 + 库存 1） | 镜像里没有 Node → compose 强制 `MCP_HARNESS_ENABLED=false` |
| **8000** | 宿主机 `uvicorn` dev | **14**（+ 外部 11） | 宿主机有 Node，外部工具可被拉起 |

**要演示外部工具接入（演示第 4 段），必须走 8000 端口的宿主机实例**；
容器实例是「离线可跑」的形态，刻意不依赖 Node。二者共用同一个数据库，数据一致。

---

## 5. MCP 接入与凭据配置

### 5.1 环境变量分级

`.env.example` 列出全部可配置项（33 个键）。按「**缺了会怎样**」分成两级：

| 级别 | 变量 | 缺失后果 |
|---|---|---|
| **必填** | `DEEPSEEK_API_KEY` | 容器能起，但**问答必定失败** |
| **必填** | `POSTGRES_PASSWORD`（可用 compose 默认值） | 数据库连不上 → api 起不来 |
| **可选 · 降级** | `HARNESS_API_KEY` | **外部工具整段不可用** → Agent 降级为纯知识库问答（演示第 4 段跳过） |
| **可选 · 降级** | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 无链路追踪回流（演示最后一段跳过，不影响其他功能） |
| **可选** | `OPENAI_API_KEY` | 仅当 `LLM_PROVIDER=openai` 时需要 |
| **可选 · 调参** | `RETRIEVER_CONFIG` | 默认 `hybrid_rerank`（向量+关键词→RRF→重排）；可切 `hybrid` / `pure_vector` |
| **可选 · 调参** | `EMBEDDING_DIM` | 默认 `512`，**必须与嵌入模型一致**；换模型须重建索引 |

### 5.2 外部工具（Harness MCP）接入

外部工具通过 MCP 协议接入，配置项都以 `MCP_` 开头：

```dotenv
MCP_ENABLED=true
MCP_HARNESS_ENABLED=true               # 容器内被 compose 强制为 false（镜像无 Node）
MCP_HARNESS_COMMAND=npx
MCP_HARNESS_ARGS=-y harness-mcp-v2@3.2.33   # ★ 锁版本，不用 @latest（会动的指针）
MCP_NPM_REGISTRY=https://registry.npmmirror.com   # 直连官方源超时，走国内镜像
MCP_HARNESS_READ_ONLY=true             # ★ 只读开关在服务端，见下
HARNESS_API_KEY=你的个人访问令牌
```

**三个容易踩的坑（都是实测换来的）**：

1. **PAT 的生成路径**：登录 Harness 平台 → `My Profile` → **`API Keys` 标签页** → `New API Key` → 其下 **`+ Token`**。
   UI 里**没有**独立的「PAT」菜单 —— 「PAT」就是「API Key 下的 Token」。
2. **个人令牌没有只读档**：平台侧不提供只读 PAT，所以「只读」是在**服务端**靠 `MCP_HARNESS_READ_ONLY=true` 实现的。
3. **npm 镜像必须配**：直连 `registry.npmjs.org` 会超时；走 `registry.npmmirror.com` 实测 **12 秒 → 0.58 秒**。

### 5.3 没有外部凭据时的降级路径

**不会起不来，只会少一段。** 演示次序与受影响段落：

| 段 | 需要外部凭据？ | 缺失时 |
|---|---|---|
| 1 架构开场 | 否 | 正常 |
| 2 知识库上传 | 否 | 正常（`make seed` 即可） |
| 3 检索问答 | 否 | 正常 |
| **4 外部系统互操作** | **是** | **跳过该段**，其余照常 |
| 5 数据分析 | 否 | 正常 |
| 6 评测 | 否 | 正常 |

### 5.4 可判定信号（别把降级当故障）

| 你想确认 | 怎么看 | 缺凭据时的**预期**现象 |
|---|---|---|
| 外部工具是否装载 | `curl localhost:18000/api/tools` | 返回体里 `by_source` 只有 `local` / `inventory`、**没有 `harness`** = **PAT 未配，这是预期行为不是 bug** |
| 预热是否就绪 | `make warmup-dry` | 退出码 **2** = 前置不满足（**不是坏了**），此时跳过演示第 4 段即可 |
| 工具是否真的能被调用 | 演示脚本第 4 段 | 无凭据时该段直接标记 SKIP |

> ⚠ **字段口径提示**：PRD §11 / §17.3 给的判据是 `select(.source=="mcp:harness")`，
> 但**实现里 `source` 取的是 MCP server 名**（本项目即 `harness`，自研示例 server 则是 `inventory`），
> 前缀 `mcp:` 目前**没有**加 —— 按 PRD 原样 grep 会永远搜不到。
> 响应体另有 `total` / `by_source` / `servers` / `tools` 四段，判断装载情况看 `total` 与 `by_source` 最直接。
> 此外 PRD 契约里的 `risk_level` 字段**当前未实现**。以上两点均为已知偏差（见 `docs/架构图.md` §7）。

> `warmup` 脚本的退出码是三档，读法不同：**0** = 通过、可直接演示；**1** = 环境坏了（要修）；
> **2** = 前置没满足（缺 Node / 缺 PAT），**换走无外部工具的演示路径即可**。

### 5.5 边界（如实登记）

- Harness **TRIAL 账号已于 2026-10-06 到期**。
- 到期后实测：**只读路径仍可用**（已实测，单次调用 1.05 ~ 2.24 秒，满足 PRD「< 3 秒」要求）。
- **写操作未验证** —— 本项目不承诺「申请个 TRIAL 就能全功能使用」，请以你自己的账号权限为准。

---

## 6. 测试与回归

```bash
make test     # pytest：单元 / 集成 / 安全 三层，152 passed / 3 skipped（跳过的是需外部服务的用例）
make verify   # 全量回归脚本（8 个脚本，每条断言逐项打印；日志落 logs/）
```

测试分层与 mock 策略：

- **单元层**：纯函数与网关逻辑，外部依赖全部替换（LLM 用 `MockLLM`，观测用 fake 客户端）。
  替换的是**最内层的单次调用**，`_call_with_failover` 这层保留 —— 否则降级逻辑根本没被执行到。
- **集成层**：通过 ASGI 传输层打真实接口；MCP 用例**真起**一个本地 server 子进程验证生命周期。
- **安全层**：注入与凭据不变量（核心断言：分词结果只允许出现实义字符）。
- 需要外部服务的用例标记为 `external`，**默认跳过**，用 `-m external` 显式运行。

覆盖率现状（如实登记）：核心链路加权 **74.5%**，全项目 **51%**；
评测/可观测链路目前 **0%**（`eval_runner` / `judge_service` / `eval_report_service` 等约 672 行）—— 属「还没测」，不是「测过没问题」。

---

## 7. 目录结构

```
AgentForge-Lite/
├── app/                    # 后端（FastAPI）
│   ├── api/                #   接口层：health / chat / documents / sessions / eval / tools
│   ├── services/           #   服务层：检索 / 解析切片 / LLM 网关 / 评测 / 观测 / 记忆
│   ├── mcp/                #   MCP 客户端与管理器（外部工具接入）
│   ├── tools/              #   内置工具与注册表
│   ├── models/             #   ORM 模型（SQLAlchemy）
│   ├── schemas/            #   请求/响应模型（Pydantic）
│   └── prompts/            #   提示词
├── frontend/               # 前端（React + TS + Ant Design，多阶段构建 → nginx）
├── mcp_servers/            # 自研 MCP server（库存查询示例）
├── scripts/                # 可执行脚本（预热 / 播种 / 各阶段验证 / 回归）
│   ├── demo_assets/        #   演示素材的源文件（PDF 的 HTML 源）
│   └── seed_demo_data.py   #   示例文档播种（PRD §17.2）
├── tests/                  # pytest：unit / integration / security 三层
├── data/sample_docs/       # 示例演示文档（PRD §17.2 的三份）
├── docs/                   # 文档：PRD / 架构图 / 教程 / 评测报告
├── Dockerfile              # 后端镜像（多阶段）
├── docker-compose.yml      # 四服务编排（db / redis / api / web）
└── Makefile                # 常用命令入口（make help）
```

## 8. 已知边界

诚实登记当前与 PRD 的差异（都已在 `docs/架构图.md` §7 逐条记录）：

1. **compose 不含 Langfuse** —— PRD F10.1 原写五个服务，但本项目 Langfuse 走云服务（自托管要多一整套 pg + clickhouse，收益为零）。
2. **api 镜像不含 Node** —— 因此容器内无法拉起外部工具；`make warmup` 只能在宿主机跑，`make up` 的启动链里**没有**预热这一步（PRD §9.8「串入 compose 启动链」未被完全满足）。
3. **对话接口为非流式** —— PRD F1.1/F1.2 要求 SSE 流式返回，当前实现是请求-响应式；前端已按「非流式」适配。
4. **示例文档为通用企业文档** —— PRD §17.2 指定的三份（考勤制度 / 产品手册 / FAQ）与评测语料口径保持一致，避免同一问题召回两份互相矛盾的规定。
5. **Harness 写操作未验证** —— TRIAL 到期后只验过只读路径（见 §5.5）。

## 9. 文档索引

| 想了解 | 看哪里 |
|---|---|
| 产品需求与技术方案（唯一权威） | [`docs/PRD-v4.1-含前沿技术.md`](docs/PRD-v4.1-含前沿技术.md) |
| 架构总览：五层职责 / 端到端数据流 / 偏差登记 | [`docs/架构图.md`](docs/架构图.md) |
| 分天实现教程（原理 → 代码 → 验证 → 面试问答） | [`docs/tutorials/README.md`](docs/tutorials/README.md) |
| 评测体系与实验结论 | [`docs/tutorials/评测体系整体说明.md`](docs/tutorials/评测体系整体说明.md) |
| 前端设计规范（双主题 / 色板 / 七页规则） | [`docs/frontend/DESIGN.md`](docs/frontend/DESIGN.md) |
