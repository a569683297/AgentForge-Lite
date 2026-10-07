# AgentForge-Lite 前端实现规格书（IMPL-SPEC）

> **这份文件是干什么的**：把"这个前端要长什么样、接哪些接口、哪些地方先别接"写成一份**可以直接照着写代码**的规格。
> 它的读者是**要实现这个前端的 AI 或人**，前提是能访问 `AgentForge-Lite` 仓库本身。
>
> 版本 **v1.0** · 2026-10-07
> 目标版本：M6（前端里程碑）
> 代码落点：`AgentForge-Lite/frontend/`（**该目录已存在，目前是空壳，见 §1.2**）

---

## 0. 给实现者的第一段话（**必读，不要跳过**）

### 0.1 你要做什么

在 `AgentForge-Lite/frontend/` 里，用 **Vite + React + TypeScript + Ant Design** 实现一个 **7 页**的中后台控制台，把已有的 Python 后端（FastAPI，14 个 REST 接口）可视化。

**这个前端的用户是工程师**，他要看的是"Agent 到底干了什么"。所以它的审美基准是**仪表盘**，不是宣传单。

### 0.2 四条已经拍板的决策（**不可协商，不要自行改**）

| # | 决策 | 具体口径 |
|---|---|---|
| 1 | **技术栈** | **Vite + React + TS + Ant Design**。**不换 Next.js**、**不引 Tailwind**、**不引 shadcn/ui** |
| 2 | **字体** | **系统字体栈**（`-apple-system / PingFang SC / …`）。**禁止引入 Inter / DM Mono**，禁止任何 Google Fonts 外链 |
| 3 | **表格行高** | **32px**（不是设计稿里的 44px）。算法见 §3.5 |
| 4 | **页面集** | **7 页**：对话台 / 知识库 / 评测看板 / 分析 / 工具注册表 / Trace 时间线 / RAG 检索检查器 |

> ⚠ 第 1 条是最容易被违反的一条。你如果"顺手"用 Next.js 或 Tailwind 会**直接作废这份交付**——因为项目的 Docker 部署、`.gitignore`、PRD 目录树全部按 Vite 写好了。

### 0.3 视觉权威的优先级（**冲突时按这个顺序裁决**）

```
docs/frontend/DESIGN.md（视觉唯一权威，684 行）
        ↓ 冲突时听它
本文档 IMPL-SPEC.md（实现规格）
        ↓ 冲突时听它
~/Downloads/agent-forge-lite（v0 产出的视觉基线稿）
```

**v0 稿是"已经实现出来的样子"，前两者是"应该是什么样"。** 三者 95% 是一致的（v0 就是把 DESIGN 实现了一遍，色板逐字相同——见 §3.1），剩下的 5% 差异已在本文 §3 逐条列出。

### 0.4 ★ 最重要的一句话：**有些页面的数据源在后端还不存在**

这是本项目的**真实状态**，不是遗漏。下表是**开工前你必须知道的分工**：

| 页面 | 后端数据源 | 你现在能做的 |
|---|---|---|
| 对话台 ChatPage | ✅ 有（但**非流式**，且工具卡片缺字段） | 能做，两个缺口见 §5.2 |
| 知识库 KnowledgePage | ✅ 完整 | 完整实现 |
| 评测看板 EvalPage | ✅ 完整（7 个接口） | 完整实现 |
| 工具注册表 ToolsPage | ⚠ 部分（无耗时/无状态） | 能做，2 个字段留空见 §5.5 |
| **分析页 AnalyticsPage** | ❌ **无 `/api/metrics`**（后端 D35 才做） | **先做界面，数据留 mock，不要编接口** |
| **Trace 时间线 TracePage** | ❌ **完全无接口** | **先做界面，数据留 mock，不要编接口** |
| **RAG 检索检查器 RetrieverPage** | ❌ 只有一段字符串，无结构化数据 | **先做界面，数据留 mock，不要编接口** |

> **红线**：**不要为了让页面对上而虚构后端接口**。遇到"没有接口"的页面，正确的做法是：
> ① 界面按 §5 的规格做出来；② 数据来自一个**集中管理的 mock 文件**（§6.5）；③ 在那个文件的字段上写明"等后端接口"。
> 这样后端接口一到位，只需替换数据获取层，界面一行不用改。

### 0.5 这份文档怎么用

- **要动手写代码**：从 §2（工程配置）→ §3（设计系统）→ §4（AntD 接入）→ §5（逐页）的顺序读。
- **想知道接什么接口**：直接跳 §6。
- **想知道"为什么不能这么做"**：§3.8 的 R01–R10 和 §8 的红线清单。
- **想知道先做哪一页**：§7.1 的实现顺序。

---

## 1. 开工前：项目现状

### 1.1 后端已经完成了什么（D01–D28）

Python / FastAPI 后端**已经全部就绪**，14 个 REST 接口可用（清单见 §6.1）。你**不需要写任何后端代码**。

技术栈（了解即可，不用碰）：

| 层 | 技术 |
|---|---|
| Web 框架 | FastAPI + Pydantic v2 |
| Agent 编排 | LangGraph（ReAct：plan → execute → observe） |
| 数据库 | PostgreSQL + pgvector |
| 缓存 | Redis |
| 可观测 | Langfuse（OpenTelemetry 体系） |
| 检索 | BM25 + 向量 双路 → RRF 融合 → bge-reranker 重排 |
| MCP | 官方 SDK，运行时动态发现外部工具 |

**后端启动方式**（你开发时要它跑着）：

```bash
# 在仓库根目录
docker compose up -d db redis          # 依赖
uv run python -m app.main              # 或 uvicorn app.main:app --reload
# → http://localhost:8000/docs 有自动生成的接口文档
```

### 1.2 `frontend/` 目录的现状

**这个目录已经存在了，但里面是空的**——只有按 PRD 目录树预留的几个**空子目录**，连一个文件都没有：

```
frontend/
└── src/
    ├── api/          ← 空
    ├── components/   ← 空
    ├── pages/        ← 空
    └── styles/       ← 空
```

两点提示：

1. **git 里目前没有 `frontend/` 的任何记录**（git 不跟踪空目录）。所以你直接往里写文件即可，**不需要清理**。
2. 这个空壳的目录名（`api` / `components` / `pages` / `styles`）**就是 PRD 定的结构**，请沿用，不要另起一套。

### 1.3 三份参考材料在哪

| 材料 | 路径 | 它的角色 | 你怎么用它 |
|---|---|---|---|
| **设计规范** | `docs/frontend/DESIGN.md`（684 行） | **视觉唯一权威**。含色板、字体、密度、AntD 主题映射（§3.6 有可直接粘贴的代码） | **必读**。§3 / §4 是本文档大量引用它的地方 |
| **v0 提示词包** | `docs/frontend/V0-PROMPT.md`（1029 行） | 之前喂给 v0 生成器的提示词。**附录 A（788 行起）是逐字核对过 schema 的数据契约** | 参考用。它的页面块（P1–P7）描述了每个页面的构成 |
| **v0 产出稿** | `~/Downloads/agent-forge-lite/` | **视觉基线**。一套真实可跑的界面，含完整 CSS | ★ **这是"要还原成什么样"的直接答案**，见 §1.4 |

#### ★ 关于 v0 产出稿：你需要知道的三个事实

那份稿子（`~/Downloads/agent-forge-lite/`）是 **Next.js + Tailwind + shadcn** 写的，**这些框架你一个都不要用**。但它有**两个可直接复用的资产**：

| 资产 | 文件 | 状态 |
|---|---|---|
| **页面结构与组件划分** | `app/page.tsx`（压缩成 129 行，实际约 914 行） | ✅ **直接照抄结构**——7 个 React 组件的骨架、每个页面的 DOM 层次 |
| **视觉样式** | `app/dashboard.css`（压缩成 5 行，实际 **221 行**） | ✅ **约 1/3 直接搬运**（自绘部分），2/3 被 AntD 组件取代（见 §4.5） |
| Tailwind / shadcn token 层 | `app/globals.css` | ❌ **完全不需要**（这个页面根本没用到它） |

> 想读原文的话，它被压缩过。`dashboard.css` 可用 `sed 's/}/}\n/g' app/dashboard.css` 展开；
> `page.tsx` 需要 prettier 才能读（`npx prettier --parser typescript app/page.tsx`）。

### 1.4 硬约束清单（**违反即作废**）

| # | 约束 | 为什么 |
|---|---|---|
| H1 | 构建工具用 **Vite**，不用 Next.js / CRA | PRD §8.3 目录树、`docker-compose.yml` 的 `web` 服务、`frontend/dist` 的 gitignore 全按 Vite 写好了 |
| H2 | 组件库用 **Ant Design** | 已在 DESIGN §9 / PRD §8.4 拍板；`DESIGN.md §3.6` 的 `ThemeConfig` 就是给它的 |
| H3 | **不引入 Tailwind CSS** | 与 AntD 的样式体系冲突；局部样式用 **CSS Modules**（类名自动加哈希、只在当前文件生效的 CSS，详见 §2.2） |
| H4 | **不引入任何外部字体** | DESIGN §4.1 明文禁止 Inter；且项目要能离线跑 |
| H5 | TypeScript，**不用 `any` 兜底** | 项目风格；接口类型见 §6.2 |
| H6 | 颜色**禁止写裸值**（`#fff` / `'red'`） | DESIGN §9 硬规则；唯一例外是 `palette.ts`。见 §8 红线 R2 |
| H7 | 主题必须走 **`<html data-theme>`** 且与 AntD `ConfigProvider` **同源** | DESIGN §9；见 §4.4。`ConfigProvider` = AntD 的**全局主题注入口**（`<ConfigProvider theme={...}>` 包住整个应用，所有 AntD 组件从它取 token） |
| H8 | **不要动 `docs/` 下的现有文档**，也不要改后端代码 | 你的工作在 `frontend/` 内闭环 |

---

## 2. 技术栈与工程配置

### 2.1 依赖清单

**运行时依赖**：

| 包 | 用途 | 备注 |
|---|---|---|
| `react` / `react-dom` | — | React 18 或 19 均可 |
| `antd` | 组件库 | ★ 必须 |
| `@ant-design/icons` | 图标 | ★ 必须。**禁止用 emoji 当图标**（DESIGN R08） |
| `axios` | HTTP 客户端 | 统一封装在 `src/api/client.ts` |
| `echarts` + `echarts-for-react` | 图表 | 用于分析页；**注意双主题问题**，见 §4.5 |

**开发依赖**：`vite`、`@vitejs/plugin-react`、`typescript`、`@types/react`、`@types/react-dom`

> **注解 · ECharts**：百度开源的图表库（柱状图 / 折线图）。PRD F9.7 / F11.6 指定用它，**不要换成别的图表库**。
> ⚠ 它**不认 AntD 的 token**——浅色/深色两套色值必须显式传进图表配置，切换主题时要重建实例（详见 §5.8）。

> **路由库**：v0 稿用的是 `useState` 切页（无路由）。**M6 阶段沿用这个做法即可**（7 个页面用状态切换，不引 react-router）。
> 理由：这是内部工具，不需要分享 URL；引路由库会增加一层与 AntD `Layout` 的集成成本。若你判断需要真路由，请**先说明理由**再改。

### 2.2 目录结构（**按此创建**）

基准是 PRD §8.3 的目录树，加上本次新增的 `theme/` 与两个新页面：

```
frontend/
├── package.json
├── vite.config.ts              ★ dev proxy 配在这里
├── tsconfig.json
├── tsconfig.node.json
├── index.html                  ★ <html data-theme="light">
└── src/
    ├── main.tsx                挂载入口
    ├── App.tsx                 应用外壳 + 7 页切换
    ├── theme/                  ★ 本次新增（§4）
    │   ├── palette.ts          色板唯一真源
    │   ├── antdTheme.ts        AntD ThemeConfig（light + dark）
    │   ├── tokens.css          CSS 变量（自绘区 + 图表用）
    │   ├── ThemeProvider.tsx   data-theme 与 ConfigProvider 同源
    │   └── global.css          body 兜底 + 焦点环
    ├── api/
    │   ├── client.ts           axios 实例（baseURL /api + 错误拦截）
    │   ├── chat.ts             POST /api/chat
    │   ├── sessions.ts         GET /api/sessions, /messages
    │   ├── documents.ts        上传 / 列表 / 删除
    │   ├── eval.ts             评测 7 个接口
    │   └── tools.ts            GET /api/tools
    ├── types/
    │   └── api.ts              ★ 后端响应的 TS 类型（照抄 §6.2）
    ├── mock/
    │   └── index.ts            ★ 无数据源页面的集中 mock（§6.5）
    ├── pages/
    │   ├── ChatPage.tsx
    │   ├── KnowledgePage.tsx
    │   ├── EvalPage.tsx
    │   ├── AnalyticsPage.tsx
    │   ├── ToolsPage.tsx
    │   ├── TracePage.tsx       ★ 新增
    │   └── RetrieverPage.tsx   ★ 新增
    ├── components/
    │   ├── AppShell.tsx        侧边栏 + 顶栏（含主题切换）
    │   ├── Sidebar.tsx
    │   ├── Topbar.tsx
    │   ├── PageHeader.tsx      h1 + 描述 + 右侧动作
    │   ├── MessageList.tsx
    │   ├── Citation.tsx        引用 [n] 按钮
    │   ├── CitationDrawer.tsx  右侧抽屉
    │   ├── ChatInput.tsx       输入区
    │   ├── ToolCallCard.tsx    工具调用卡片（可折叠）
    │   ├── StatusDot.tsx       8px 状态色点
    │   ├── MetricCard.tsx      指标卡
    │   ├── MetricChart.tsx     ECharts 封装（双主题）
    │   ├── Skeleton.tsx        骨架屏（形状要对齐真实内容）
    │   └── EmptyState.tsx      空态
    └── styles/
        ├── shell.module.css        外壳布局
        ├── chat.module.css         对话页自绘部分
        ├── eval.module.css         评测页自绘部分
        ├── tools.module.css        工具页自绘部分
        └── trace.module.css        Trace 页自绘部分
```

> **目录树里出现的两个词，先注解掉**：
> - **dev proxy（开发代理）** —— 开发时让 Vite 把 `/api` 请求转发到后端 `:8000` 的技术，用来绕开浏览器的**跨域限制**。展开见 §2.3。
> - **骨架屏（skeleton）** —— 加载时先画出内容的**灰色轮廓**，让人知道"这里将出现什么"，而不是转圈或写 `Loading...`。形状必须对齐真实内容（§3.7）。
>
> **注解 · CSS Modules**：Vite 内置支持。文件名写成 `xxx.module.css`，在组件里 `import s from './shell.module.css'`，然后 `className={s.sidebar}`。
> 它的作用是把类名自动加哈希（编译后变成 `.shell_sidebar__a1b2c`），**保证样式只在本组件生效**，不会互相污染。
> 这是"不引 Tailwind 又要写自定义样式"的官方解法。

### 2.3 `vite.config.ts`（dev proxy 必须配）

> **注解 · dev proxy**：开发时前端跑在 `:5173`，后端在 `:8000`，浏览器直接请求 `:8000` 会被**跨域策略（CORS）**拦住。
> dev proxy 让 Vite 开一个中转：前端请求 `/api/xxx`（同源，不触发跨域），Vite 偷偷转发给 `:8000`。

```ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
```

**这样配完之后，前端代码里一律写相对路径** `/api/health`，**不要写** `http://localhost:8000/api/health`——否则生产环境（nginx 同源）会挂。

### 2.4 入口三件套

**`index.html`**（注意 `data-theme` 与防闪白）：

```html
<!doctype html>
<html lang="zh-CN" data-theme="light">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AgentForge · Agent 运行时平台</title>
    <script>
      // 在首屏渲染前把主题定下来，避免"先白后黑"的闪烁
      // （这一段必须是内联同步脚本，不能放到 React 里）
      const t = localStorage.getItem('af-theme');
      if (t === 'dark' || (!t && matchMedia('(prefers-color-scheme: dark)').matches)) {
        document.documentElement.setAttribute('data-theme', 'dark');
      }
    </script>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

> v0 稿里挂了一个 `@vercel/analytics`，**不要搬过来**——那是 v0 平台自带的，与项目无关。

**`src/main.tsx`**：

```tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ThemeProvider } from './theme/ThemeProvider';
import App from './App';
import './theme/tokens.css';
import './theme/global.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
);
```

**`src/App.tsx`**：应用外壳 + 页面切换。结构参照 v0 稿的 `page.tsx`（第 72–240 行），但把 `dark` 状态换成 `ThemeProvider` 的 context、把 `.app-shell` 那一层换成 AntD `<Layout>`。

页面键与中文标题（**顺序即导航顺序**）：

```ts
export type PageKey =
  | 'chat'        // 对话台      MessageSquare
  | 'knowledge'   // 知识库      Database
  | 'eval'        // 评测看板    FlaskConical
  | 'analytics'   // 分析        BarChart3
  | 'tools'       // 工具注册表  Wrench
  | 'trace'       // Trace 时间线 Activity      ← 新增
  | 'retriever';  // 检索检查器  Search          ← 新增
```

> 图标全部取自 `lucide-react`（v0 稿用的库）。落地时**换成 `@ant-design/icons` 的等价图标**（H2 约束）：
> 例如 `MessageSquare` → `<MessageOutlined />`、`Database` → `<DatabaseOutlined />`、`FlaskConical` → `<ExperimentOutlined />`、
> `BarChart3` → `<BarChartOutlined />`、`Wrench` → `<ToolOutlined />`、`Activity` → `<FundProjectionScreenOutlined />`、`Search` → `<SearchOutlined />`。

### 2.5 命令

```bash
cd frontend
npm install            # 或 pnpm install / yarn
npm run dev            # → http://localhost:5173
npm run build          # 产物在 frontend/dist（已被 .gitignore 忽略）
npm run preview        # 预览构建产物
npx tsc --noEmit       # 类型检查（提交前必须跑）
```

---

## 3. 设计系统（视觉规范）

> 本章内容以 `docs/frontend/DESIGN.md` 为准。凡是本章与 DESIGN.md 不一致的地方，**以 DESIGN.md 为准**，并请在交付时提出来。

### 3.0 先把词说清

| 名词 | 一句话解释 |
|---|---|
| **设计 token（设计令牌）** | 一个带名字的样式最小单位，如"正文颜色 = `#14181E`"。意思是这个决定**只在一处定义**，全站引用。好处：改一个值全站跟着变，且 AI 无法"自己挑一个蓝色" |
| **canvas / surface / sunken** | 三个中性底层级。`canvas` = 页面最底那层；`surface` = 浮在上面的卡片；`sunken` = 凹进去的区域（代码块、表头、输入框底） |
| **tabular-nums** | 字体的一种数字排版开关。打开后**每个数字等宽**，数字可以上下对齐。关着时 `1111` 比 `8888` 窄，表格里的数字会参差不齐 |
| **span（跨度）** | 一次 Agent 运行被拆成的步骤，每步（一次 LLM 调用 / 一次检索 / 一次工具调用）叫一个 span |
| **骨架屏（skeleton）** | 加载时先画出内容的灰色轮廓，让人知道"这里将出现什么"，而不是转圈或写 `Loading...` |
| **WCAG AA** | 国际无障碍规范最常见的一档：正文文字与背景的对比度至少 **4.5 : 1**。低于这个值，弱视和低质量屏幕上会看不清 |

### 3.1 色板

**这是整套视觉的基础。色值取自 DESIGN §3.2，与 v0 稿逐字相同（已核对）。**

#### 中性层

| 语义 | Light | Dark | 用途 |
|---|---|---|---|
| `canvas` | `#F5F6F8` | `#131519` | 页面最底层 |
| `surface` | `#FFFFFF` | `#191C21` | 卡片、面板、侧边栏、顶栏 |
| `surface-raised` | `#FFFFFF` | `#1F2329` | 浮层（modal / dropdown / tooltip） |
| `sunken` | `#ECEEF1` | `#0E1013` | 凹入区：代码块、表头、输入框底、时间线轨道 |
| `border-subtle` | `#E4E7EB` | `#2A2F36` | 分隔线、表格横线 |
| `border-default` | `#D5D9DF` | `#33383F` | 卡片 / 控件描边 |
| `border-strong` | `#B8BEC7` | `#454B54` | hover / 聚焦描边 |

#### 文字层

| 语义 | Light | Dark | 对比度（实测） | 用途 |
|---|---|---|---|---|
| `text-primary` | `#14181E` | `#F2F3F5` | 17.8:1 · 15.4:1 | 正文、标题 |
| `text-secondary` | `#4A5361` | `#A8B0BC` | 7.8:1 · 7.8:1 | 次要说明、表头、导航未选中态 |
| `text-tertiary` | `#667080` | `#7B8494` | 5.0:1 · 4.5:1 | 元信息、时间戳、placeholder |
| `text-disabled` | `#949CA8` | `#5A626E` | 2.8:1 · 2.8:1 | **仅限禁用态** |

> ⚠ **`text-disabled` 不达 AA（2.8:1），这是有意的取舍**——禁用态本来就该"看起来不能点"。
> **但它带来一条硬规则：任何承载信息的文字，一律禁止使用 `text-disabled`。**
> 输入框的 placeholder 用 `text-tertiary`，**不要为了"看着像占位符"把对比度压到 3 以下**。

#### 语义色 + 强调色

| 语义 | Light | Dark | 对比度（Light / Dark） |
|---|---|---|---|
| **`accent`** | **`#0E6F63`** | **`#4FD1B8`** | 6.05:1 · 9.07:1 |
| `on-accent` | `#FFFFFF` | `#0A2A24` | 6.05:1 · 8.14:1 |
| `success` | `#0B7A46` | `#3DD68C` | 5.40:1 · 9.11:1 |
| `danger` | `#C0271D` | `#FF7A6E` | 5.92:1 · 6.72:1 |
| `warning` | `#8A5A08` | `#E0A83A` | 5.92:1 · 8.00:1 |
| `info` | `#0B6BA8` | `#5FB4E8` | 5.70:1 · 7.47:1 |

**全部通过 WCAG AA。** 上表每个数字都是脚本实算的。

**v0 稿里额外有两个值，DESIGN 没有，但要保留：**

| 名 | Light | Dark | 用途 |
|---|---|---|---|
| `accent-soft` | `#E2F2EF` | `#173B36` | **选中态底色**（导航当前项、选中的 run 卡、激活的筛选按钮） |
| `accent-hover` | `#0A574E` | `#6FE0CB` | 主按钮 hover |

> ⚠ v0 稿的主按钮 hover 用的是 `filter: brightness(.94)`，**不要照搬**。
> 原因：`filter` 会连带把按钮内**所有子元素**（图标、文字）一起调暗，是隐患。**用实色 `accent-hover`。**

**刻意避开的四类颜色**（不要"顺手"用它们）：

| 避开 | 为什么 |
|---|---|
| Tailwind `blue-600 #2563EB` | v0 / shadcn 默认主色，撞车率最高 |
| `indigo-500 #6366F1` 及一切紫蓝渐变 | "AI 生成"的视觉签名 |
| **AntD `#1677FF`** | 全网 AntD 系统的默认主色，**用它等于"没改过主题"** |
| Dify 电光蓝 `#0033FF` | 抄了就是"仿 Dify" |

#### ★ 语义色使用纪律（三条，容易被忽略）

1. **`accent`（青）只用于交互**：当前选中项、链接、焦点环、主按钮。**永不用于表达状态。**
2. **`success`（绿）只用于状态**，永不用于交互。
3. → 因为**青与绿在同一色相区**，**同一条信息里不得同时出现 accent 与 success**，否则用户分不清"这是我能点的"还是"这是已经好的"。
4. 语义色**只用色点 + 文字**，**禁止铺成色块底**（见 §3.8 的 R06）。

> ⚠ 附注（避免误判）：**评测看板的"通过 / 失败"用 `success` 绿 / `danger` 红**，这是国际开发者工具（JUnit / CI / Grafana）的通用惯例。
> 这与"中国股市红涨绿跌"**完全不冲突**，两者是不同语境，**不要改**。

### 3.2 ★ v0 变量名 ↔ DESIGN 变量名 映射表

**这一节必须逐行看。** v0 稿和 DESIGN.md 用的是**同一套色值，但变量名不同，且描边那一档差了一位**——照着 v0 的 CSS 抄而不换名，会得到一个颜色偏浅的界面。

| v0 稿的变量 | v0 的值（Light） | **应改用 DESIGN 的** | 说明 |
|---|---|---|---|
| `--canvas` | `#f5f6f8` | `--af-canvas` | ✅ 名字不同，值同 |
| `--surface` | `#fff` | `--af-surface` | ✅ |
| `--surface-raised` | `#fff` | `--af-surface-raised` | ✅ |
| `--sunken` | `#eceef1` | `--af-sunken` | ✅ |
| `--border` | `#e4e7eb` | `--af-border-subtle` | ⚠ **名字不同** |
| **`--border-strong`** | `#d5d9df` | **`--af-border-default`** | ⚠⚠ **差一档！** v0 的"strong"其实是 DESIGN 的"default" |
| （v0 无此变量） | — | `--af-border-strong` (`#B8BEC7`) | v0 只有两档描边，DESIGN 有三档 |
| `--text` | `#14181e` | `--af-text-primary` | ⚠ 名字不同 |
| `--secondary` | `#4a5361` | `--af-text-secondary` | ✅ |
| `--tertiary` | `#667080` | `--af-text-tertiary` | ✅ |
| （v0 无此变量） | — | `--af-text-disabled` (`#949CA8`) | — |
| `--accent` | `#0e6f63` | `--af-accent` | ✅ |
| `--accent-soft` | `#e2f2ef` | ★ **DESIGN 没有，补进 `palette.ts`** | 见 §4.1 |
| （v0 无此变量） | — | `--af-accent-hover` (`#0A574E`) | v0 用 `filter`，见 §3.1 |
| `--success` / `--danger` / `--warning` / `--info` | 同 DESIGN | `--af-success` / … | ✅ 值全等 |

**暗色同理，且 v0 同样差一档**（v0 `--border-strong: #33383f` = DESIGN `--af-border-default`）。

**执行方式**：**统一使用 DESIGN 的命名（`--af-*`）**，因为 AntD 主题（§4.2）也从同一份色板读。
从 v0 的 CSS 搬样式时，把 `var(--border)` 换成 `var(--af-border-subtle)`、`var(--border-strong)` 换成 `var(--af-border-default)`——**逐条换，不要用全局替换**（那两个名字在 v0 里各自出现在不同位置）。

### 3.3 字体（**硬约束 H4**）

```css
--af-font-sans: -apple-system, BlinkMacSystemFont, 'SF Pro Text',
                'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
--af-font-mono: 'SF Mono', 'JetBrains Mono', Menlo, Consolas,
                'Noto Sans Mono CJK SC', monospace;
```

**v0 稿的第一行是这样的**（必须删掉）：

```css
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Inter:wght@400;500&display=swap');
```

**为什么必须删**（两条独立的理由）：

1. **DESIGN §4.1 明文**：「不要引入 Inter。Inter 已经是 v0 / shadcn 的事实默认字体——**用它等于自动加入平均值**」。系统字体栈在用户机器上天然无特征、**零加载成本、渲染最快**。
2. **项目要能离线跑**：外链字体在断网 / 内网环境下会静默回退，且多两个请求。

> ⚠ **AntD 默认字体栈里没有 `'PingFang SC'`。** 在 macOS 上 `-apple-system` 会回退到苹方，看着没问题；但在没配好中文回退的环境里，中文字形会跳到别的字体上去。
> 所以 §4.2 的 `fontFamily` **必须显式覆盖**。

### 3.4 字号与字重

| 角色 | 字号 / 行高 | 字重 | AntD 对应 token |
|---|---|---|---|
| 页面标题 h1 | 20 / 28 | 500 | 自定义（见 §5.1 的 `PageHeader`） |
| 区块标题 h2 | 16 / 24 | 500 | `fontSizeHeading5`(16) |
| 卡片标题 h3 | 14 / 20 | 500 | 自定义 |
| **正文（基准）** | **13 / 20** | 400 | `fontSize`(13) |
| 元信息 | 12 / 16 | 400 | `fontSizeSM`(11) |
| 代码 / ID / 数字 | 12 / 16 **mono** | 400 | `fontFamilyCode` |
| 最小字号 | 11 | 400 | `fontSizeSM`(11) |

**字重只有 400 和 500 两档**，不用 600 / 700（AntD 的 `fontWeightStrong` 默认 600，已在 §4.2 改为 500）。

> **把基准字号压到 13px，是把"仪表盘"和"落地页"分开的第一刀。**
> AI 生成的界面默认 16px 基准、h1 36px 起步；AntD 默认 14px / h1 38px —— 都偏大。

#### 等宽数字（**强制**）

任何**会被纵向比较**的数字都必须：

```css
.af-num {
  font-family: var(--af-font-mono);
  font-variant-numeric: tabular-nums;
}
```

**适用范围**：耗时（ms）、token 数、成本（¥）、得分、run id、trace id、span id、指纹（如 `a7797b06…`）。**表格里的数字列右对齐。**

> ⚠ **AntD 的 `Table` 不会自动给数字加 `tabular-nums`。** 要在列的 `render` 里套一层 `<span className="af-num">`，或用 `columns[].className` 配全局样式。

### 3.5 间距与密度

**4px 基准网格** —— 一切间距是 4 的倍数（与 AntD 的 `sizeUnit: 4` 一致，不用改）。

| 对象 | 目标值 | AntD 默认 | 是否要改 |
|---|---|---|---|
| 页面内边距 | 20 / 24（v0 实测 `padding: 20px 24px`） | — | — |
| 区块间距 | 16 / 24 / 32 三档 | — | — |
| 卡片内边距 | 12（紧凑）/ 16（默认） | Card body 默认 **24** | ⚠ **偏大，按需覆盖** |
| 控件高度 | 28（小）/ **32（默认）** / 36 | `controlHeight` **32** | ✅ 一致 |
| 列表行高 | 28（紧凑）/ **32（默认）** | Table 约 **54** | ⚠⚠ **必须覆盖** |
| 表格单元格 padding | 上下 **6** / 左右 **12** | 上下 **16** / 左右 16 | ⚠⚠ **必须覆盖** |

#### ★ 行高 32px 是怎么来的（**决策 3 的落地算法**）

**行高 = 上下 padding × 2 + 行高（lineHeight）**

```
目标 32px  →  cellPaddingBlock = 6      （6 × 2 + 20 = 32）  ← 采用这个
目标 28px  →  cellPaddingBlock = 4      （4 × 2 + 20 = 28）
AntD 默认  →  cellPaddingBlock = 16     （16 × 2 + 20 = 52，实测约 54px）
```

#### ⚠ v0 稿在这里有一处冗余，必须删掉

v0 的 CSS（第 100 行、134 行）写的是：

```css
td { height: 44px; padding: 6px 12px; ... }          /* ← height 是多余的 */
.compact-table td { height: 40px; }                  /* ← 同上 */
```

**注意：v0 的 padding 其实已经是"上下 6 / 左右 12"，与 DESIGN 的目标一致。**
真正把行高顶到 44px 的是那句**显式 `height: 44px`**。

> **✅ 所以落地方式很简单：搬 v0 的表格样式时，把 `td` / `.compact-table td` 上的 `height` 声明整条删掉**，
> 行高会自然落到 `6 × 2 + 20 = 32px`，**恰好符合 §5.2 的目标，不需要再手写高度**。
> 若你用的是 AntD `<Table>`，则不用管 `td`——直接靠 §4.2 的 `cellPaddingBlock: 6`。

#### 密度守门线（**验收时能数的，不是"感觉"**）

| 页面 | 硬指标 |
|---|---|
| Trace 时间线 | 1440×900 视口内，首屏可见 **≥ 12 个 span** |
| 评测看板 | 首屏可见 **≥ 15 行**题目结果 |
| 工具注册表 | 首屏可见 **≥ 10 个工具** |
| 对话台 | 单条消息（含工具调用卡片）**折叠后**高度 **≤ 72px** |

### 3.6 形状、层次、焦点

**圆角只有两档，上限 6px**：`容器 6px` · `控件 4px` · `标签 4px`。
**禁止 ≥ 8px 的圆角，禁止胶囊按钮。**

**层级表达顺序（严格按此顺序，不要颠倒）**：

1. **背景微差** —— `canvas` 与 `surface` 的差别（`#F5F6F8` vs `#FFFFFF`）
2. **1px 描边** —— `border-subtle` / `border-default`
3. **阴影** —— **只在浮层用**（dropdown / modal / drawer / tooltip），且必须是 §4.2 里那个**单层**阴影

> **卡片一律不加阴影，加描边。** 这是 R02。
> ✅ 好消息：AntD 的 `Card` 默认就是"白底 + 1px 描边、无阴影"，**这一点不用改**。真正要管的是**别自己往上加 `boxShadow`**。

**焦点环**（无障碍要求，也是"这是个真工具"的信号）：

```css
:focus-visible {
  outline: 2px solid var(--af-accent);
  outline-offset: 1px;
}
```

**禁止 `outline: none` 而不提供替代。**

### 3.7 状态与反馈

**这一节是 AI 生成界面最偷懒的地方，也是最能拉开差距的地方。**

| 状态 | 要求 | AntD 对应 |
|---|---|---|
| **加载** | **骨架屏，形状必须与真实内容一致**（表格骨架就画 5 行格子，不是居中转圈） | `<Skeleton active />`，或 `<Table loading={{ spinning, indicator }}>` + 自定义骨架 |
| **空** | 一句人话 + **一个具体动作**。例：「还没有评测记录。跑一次评测 →」（箭头是可点按钮） | `<Empty>` 的 `description` **要重写**，**不要用默认插画**（默认插图是 AI 感的来源之一） |
| **错误** | 必须给**可操作的下一步**，不能只贴报错原文。例：「Reranker 模型未找到。检查 `models/bge-reranker-base` 是否存在 →」 | `<Result status="error">` 或 `<Alert type="error">`，`action` 必须给 |
| **流式输出** | 光标用 **2px 竖条**。禁止打字机音效、禁止逐字缩放动画 | 自绘 |
| **禁用** | 用 `text-disabled`；降不透明度**只作用于背景**，文字保持可读 | AntD 默认已符合 |
| **长时运行** | 超过 2 秒**必须有进度或耗时显示**（"已运行 3.4s"），不能只有一个转圈 | `<Spin>` 只给 `<Button loading>` 用；页面级用进度条 + 计时文本 |

**动效预算**：只允许 **150–200ms** 的 `opacity` / `transform` 过渡。
**禁止**入场动画、禁止列表 stagger（逐个延迟出现）、禁止任何循环播放的装饰动画。
**唯一例外**：运行中的进度条（v0 的 `.progress-line` 那个流动动画可以保留）。

> ✅ AntD 的 `motionDurationMid` 默认 `0.2s` = 200ms，**在预算内，不用改**。`motionDurationSlow` 是 `0.3s`，被 Modal / Drawer 这类面板动画使用，**可接受**。

### 3.8 十条硬规则（"不许"清单）

**这一节是整份规范里最值钱的部分。** 有开发者实测：光是给 AI 一份明确的禁止清单，就能砍掉大约八成通用输出。

| # | 规则 | 为什么 |
|---|---|---|
| **R01** | **先密度，后留白。** 一屏（1440×900）至少 20 行有效信息；列表行高上限 36px | AI 默认吐 48px+ 行高的"透气"版式，那是落地页的密度 |
| **R02** | **层级用「背景微差 + 1px 描边」表达，不用阴影** | 阴影是 AI 生成界面最显眼的同质化特征 |
| **R03** | **圆角只有两档：容器 6px、控件 4px。禁止 ≥ 12px，禁止胶囊按钮** | 8/12/16px 圆角 + 胶囊是 shadcn 默认长相 |
| **R04** | **强调色像素面积 ≤ 全屏 3%。** 只给"当前选中 / 链接 / 焦点环 / 主按钮" | 大色块是"AI 感"的第二来源 |
| **R05** | **所有数字用等宽字体 + `tabular-nums`，表格里右对齐** | 耗时、token 数、分数必须能竖着比 |
| **R06** | **状态用 8px 色点 + 文字表达，禁止用整块彩色背景** | 彩色背景块是"AI 生成 dashboard"的典型脸 |
| **R07** | **禁止渐变。** 唯一例外：流式输出的光标与进度条 | 渐变 hero 是 AI 感的第一大来源 |
| **R08** | **禁止用 emoji 当图标**（用 `@ant-design/icons`，且**同层级图标尺寸统一**） | 一看就是生成的 |
| **R09** | **空 / 加载 / 错误三态必须真实设计**，禁止 `Loading...`、禁止空白页 | AI 只给你"数据齐全时好看"的那一屏 |
| **R10** | **禁止 hero 结构**（居中大标题 + 副标题 + 两个按钮） | 你不是在卖东西 |

---

## 4. AntD 主题接入（★ 先做这一步）

> **为什么必须先做**：不做这一步，后面每一页都会有人直接往组件里写 `#fff`。**主题骨架是"防止样式退化"的地基。**
> 这也是 `DESIGN.md §10` 落地顺序的第 1 步。

> **注解 · AntD 主题的四个概念**（先看这个，再看代码）：
> - **Seed Token（种子令牌）** = 源头，比如 `colorPrimary`。AntD 用算法从它推出整条色阶。
> - **Map Token（梯度令牌）** = 算法派生出来的值，比如 `colorPrimaryHover`、`colorBgContainer`。
> - **Alias Token（别名令牌）** = 语义别名，比如 `colorTextDisabled`、`colorTextPlaceholder`。
> - **Component Token（组件令牌）** = 只作用于某一个组件的 token，写在 `components: { Table: {...} }` 里。
>
> **三层是自动派生关系**——改 Seed 会连锁改 Map 和 Alias。理解这点才能预判"改一个值会连带改什么"。

### 4.1 `src/theme/palette.ts`（色板唯一真源）

**为什么要有它**：色板会有**三处**落地——AntD token、CSS 变量、ECharts 图表配置。
**绝不能手抄三份**，否则改色时必漏一处。所有地方都从这里读。

```ts
// src/theme/palette.ts —— 色板的唯一真源。改色只改这里。
export const palette = {
  light: {
    canvas: '#F5F6F8',
    surface: '#FFFFFF',
    surfaceRaised: '#FFFFFF',
    sunken: '#ECEEF1',

    borderSubtle: '#E4E7EB',
    borderDefault: '#D5D9DF',
    borderStrong: '#B8BEC7',

    textPrimary: '#14181E',
    textSecondary: '#4A5361',
    textTertiary: '#667080',
    textDisabled: '#949CA8',

    accent: '#0E6F63',
    accentHover: '#0A574E',
    accentSoft: '#E2F2EF',   // ★ DESIGN 没有，取自 v0 稿（选中态底色）
    onAccent: '#FFFFFF',

    success: '#0B7A46',
    danger: '#C0271D',
    warning: '#8A5A08',
    info: '#0B6BA8',

    radiusContainer: 6,
    radiusControl: 4,
    shadowOverlay: '0 4px 12px rgb(0 0 0 / 0.08)',
  },
  dark: {
    canvas: '#131519',
    surface: '#191C21',
    surfaceRaised: '#1F2329',
    sunken: '#0E1013',

    borderSubtle: '#2A2F36',
    borderDefault: '#33383F',
    borderStrong: '#454B54',

    textPrimary: '#F2F3F5',
    textSecondary: '#A8B0BC',
    textTertiary: '#7B8494',
    textDisabled: '#5A626E',

    accent: '#4FD1B8',
    accentHover: '#6FE0CB',
    accentSoft: '#173B36',
    onAccent: '#0A2A24',

    success: '#3DD68C',
    danger: '#FF7A6E',
    warning: '#E0A83A',
    info: '#5FB4E8',

    radiusContainer: 6,
    radiusControl: 4,
    shadowOverlay: '0 4px 12px rgb(0 0 0 / 0.4)',
  },
} as const;

export type ThemeMode = keyof typeof palette;
```

### 4.2 `src/theme/antdTheme.ts`（可直接用）

**这份配置来自 `DESIGN.md §3.6`，那里逐个核对了 AntD 源码的 `ComponentToken` 接口与 `prepareComponentToken` 默认值。**

```ts
// src/theme/antdTheme.ts
import type { ThemeConfig } from 'antd';
import { palette } from './palette';

const fontSans =
  "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', " +
  "'Microsoft YaHei', system-ui, sans-serif";
const fontMono =
  "'SF Mono', 'JetBrains Mono', Menlo, Consolas, 'Noto Sans Mono CJK SC', monospace";

export const lightTheme: ThemeConfig = {
  token: {
    // ── 品牌与语义色（Seed）──────────────────────────
    colorPrimary:  palette.light.accent,   // #0E6F63  ← 替掉 AntD 默认 #1677FF
    colorLink:     palette.light.accent,
    colorSuccess:  palette.light.success,
    colorWarning:  palette.light.warning,
    colorError:    palette.light.danger,
    colorInfo:     palette.light.info,

    // ── 中性底（Map：必须显式覆盖，否则继承默认灰 #f5f5f5）──
    colorBgLayout:        palette.light.canvas,        // #F5F6F8
    colorBgContainer:     palette.light.surface,       // #FFFFFF
    colorBgElevated:      palette.light.surfaceRaised,
    colorBorder:          palette.light.borderDefault, // 默认 #d9d9d9
    colorBorderSecondary: palette.light.borderSubtle,  // 默认 #f0f0f0

    // ── 文字（Alias）─────────────────────────────────
    // ⚠ 故意用「实色」而不是 AntD 默认的 rgba(0,0,0,.88) 那套透明黑。
    //   原因：透明黑叠在 canvas 上和在 surface 上会呈现两种不同的灰，
    //   导致"同一个 text-secondary 在两处看起来不一样"。用固定色阶换掉它。
    colorText:            palette.light.textPrimary,
    colorTextSecondary:   palette.light.textSecondary,
    colorTextTertiary:    palette.light.textTertiary,
    colorTextQuaternary:  palette.light.textDisabled,
    colorTextDisabled:    palette.light.textDisabled,
    colorTextPlaceholder: palette.light.textTertiary,   // ← 默认 0.25 黑不达 AA，见 §3.1

    // ── 密度（与 AntD 出厂值差别最大的一段）──
    fontSize:      13,   // 默认 14        ← §3.4 基准字号
    fontSizeSM:    11,   // 默认 12
    fontSizeLG:    15,   // 默认 16
    controlHeight:  32,  // 默认 32  ✅ 一致
    controlHeightSM: 26, // 默认 24
    controlHeightLG: 36, // 默认 40
    borderRadius:    6,  // 默认 6   ✅ 一致
    borderRadiusSM:  4,  // 默认 4   ✅ 一致
    borderRadiusLG:  6,  // 默认 8   ← 必须改！Card / Modal 走这个
    borderRadiusXS:  2,
    lineHeight:   1.54,  // 默认 1.5714；13px × 1.54 ≈ 20px 行高

    // ── 字重：AntD 默认 600，本规范只要 400 / 500 ──
    fontWeightStrong: 500, // 默认 600

    // ── 阴影：压掉 AntD 默认的三层堆叠，只留浮层单层（R02）──
    boxShadow:          palette.light.shadowOverlay,
    boxShadowSecondary: palette.light.shadowOverlay,
    boxShadowTertiary:  '0 1px 2px rgb(0 0 0 / 0.04)',

    // ── 字体 ─────────────────────────────────────────
    fontFamily:     fontSans,   // AntD 默认不含 'PingFang SC'，中文字形会飘
    fontFamilyCode: fontMono,

    // ── 焦点环（§3.6）─────────────────────────────────
    lineWidthFocus:      2,
    controlOutlineWidth: 2,
  },

  components: {
    // ★ Layout：AntD 中后台感的最大来源就是这两个 #001529
    Layout: {
      headerBg:  palette.light.surface,   // 默认 '#001529' ← 深蓝黑顶栏，必须改
      siderBg:   palette.light.surface,   // 默认 '#001529'
      bodyBg:    palette.light.canvas,    // 默认 colorBgLayout
      headerHeight:  48,                  // 默认 controlHeight×2 = 64 ← 落地页高度
      headerPadding: '0 16px',            // 默认 controlHeightLG×1.25 = 50px
    },

    // ★ Table：行高从默认约 54px 压到 32px 的地方（§3.5）
    Table: {
      headerBg:         palette.light.canvas,          // 默认 colorFillAlterSolid
      headerColor:      palette.light.textSecondary,   // 表头用次级文字，不用主文字
      headerSplitColor: 'transparent',                 // 去掉表头竖分隔线
      borderColor:      palette.light.borderSubtle,
      rowHoverBg:       palette.light.canvas,
      cellFontSize:     13,  // 默认 fontSize
      cellFontSizeMD:   13,
      cellFontSizeSM:   12,
      // 行高算法：cellPaddingBlock×2 + lineHeight(20) = 目标行高
      //   目标 32px → cellPaddingBlock = 6（AntD 默认 16 → 行高约 54px）
      cellPaddingBlock:    6,
      cellPaddingInline:   12,   // 默认 16
      cellPaddingBlockMD:  4,    // Table size="middle"
      cellPaddingInlineMD: 8,
      cellPaddingBlockSM:  2,    // Table size="small"
      cellPaddingInlineSM: 8,
    },
  },
};

// 深色主题：结构完全相同，色值换成 palette.dark
// （建议抽一个 buildTheme(mode) 函数，避免两份配置漂移——见下方"实现提示"）
export const darkTheme: ThemeConfig = { /* 同结构，色值取 palette.dark */ };
```

**实现提示（强烈建议）**：不要把上面两份配置手抄两遍。写一个函数：

```ts
function buildTheme(mode: ThemeMode): ThemeConfig {
  const p = palette[mode];
  const shadow = p.shadowOverlay;
  return {
    token: {
      colorPrimary: p.accent,
      colorBgLayout: p.canvas,
      // ... 全部从 p 读，一处不落
      boxShadow: shadow,
      boxShadowSecondary: shadow,
    },
    components: {
      Layout: { headerBg: p.surface, siderBg: p.surface, bodyBg: p.canvas, headerHeight: 48, headerPadding: '0 16px' },
      Table: { headerBg: p.canvas, headerColor: p.textSecondary, borderColor: p.borderSubtle, rowHoverBg: p.canvas, cellPaddingBlock: 6, cellPaddingInline: 12 },
    },
  };
}
export const lightTheme = buildTheme('light');
export const darkTheme  = buildTheme('dark');
```

> **⚠ 关于 `components` 里的 token 名**：组件级 token 名称**会随 AntD 版本变动**。落代码前花一分钟核对：
> ```bash
> cat node_modules/antd/es/table/style/index.d.ts   # 找 ComponentToken 接口
> cat node_modules/antd/es/layout/style/index.d.ts
> ```
> **以你实际安装的版本为准。** 如果某个名字不存在，TS 会报错——这是好事，别用 `as any` 压掉。
>
> **本文档只列了 Layout / Table 两个组件。** 其他组件（`Button` / `Card` / `Select` / `Tag` / `Statistic`…）**按需扩写**。
> 扩写原则：**先查该组件的 `ComponentToken`，只改默认值里有问题的项**，不要为了"统一"而全量覆盖（全量覆盖会让你升级 AntD 时错过它的修复）。

### 4.3 `src/theme/tokens.css`（CSS 变量）

**用途收窄为三处**（AntD 组件自己不读它）：

1. **自绘区域** —— 如 Trace 页的横向 span 图（AntD 没这个组件）
2. **图表配色** —— ECharts 不认 AntD 的 token，要显式传色值
3. **全局兜底** —— `<body>` 这个层级 AntD 管不到

```css
/* tokens.css —— 自绘区域与图表的色彩来源。
   ⚠ 与 antdTheme.ts 是同一份色板的两处落地，改色必须两处一起改（都从 palette.ts 派生最保险） */

:root,
html[data-theme='light'] {
  --af-canvas: #F5F6F8;
  --af-surface: #FFFFFF;
  --af-surface-raised: #FFFFFF;
  --af-sunken: #ECEEF1;

  --af-border-subtle: #E4E7EB;
  --af-border-default: #D5D9DF;
  --af-border-strong: #B8BEC7;

  --af-text-primary: #14181E;
  --af-text-secondary: #4A5361;
  --af-text-tertiary: #667080;
  --af-text-disabled: #949CA8;

  --af-accent: #0E6F63;
  --af-accent-hover: #0A574E;
  --af-accent-soft: #E2F2EF;   /* ★ v0 稿有、DESIGN 无，保留 */
  --af-on-accent: #FFFFFF;

  --af-success: #0B7A46;
  --af-danger: #C0271D;
  --af-warning: #8A5A08;
  --af-info: #0B6BA8;

  --af-font-sans: -apple-system, BlinkMacSystemFont, 'SF Pro Text',
                  'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
  --af-font-mono: 'SF Mono', 'JetBrains Mono', Menlo, Consolas,
                  'Noto Sans Mono CJK SC', monospace;

  --af-radius-container: 6px;
  --af-radius-control: 4px;
  --af-shadow-overlay: 0 4px 12px rgb(0 0 0 / 0.08);
}

html[data-theme='dark'] {
  --af-canvas: #131519;
  --af-surface: #191C21;
  --af-surface-raised: #1F2329;
  --af-sunken: #0E1013;

  --af-border-subtle: #2A2F36;
  --af-border-default: #33383F;
  --af-border-strong: #454B54;

  --af-text-primary: #F2F3F5;
  --af-text-secondary: #A8B0BC;
  --af-text-tertiary: #7B8494;
  --af-text-disabled: #5A626E;

  --af-accent: #4FD1B8;
  --af-accent-hover: #6FE0CB;
  --af-accent-soft: #173B36;
  --af-on-accent: #0A2A24;

  --af-success: #3DD68C;
  --af-danger: #FF7A6E;
  --af-warning: #E0A83A;
  --af-info: #5FB4E8;

  --af-shadow-overlay: 0 4px 12px rgb(0 0 0 / 0.4);
}
```

**`src/theme/global.css`**（body 兜底 + 焦点环）：

```css
* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: var(--af-font-sans);
  font-size: 13px;
  line-height: 20px;
  color: var(--af-text-primary);
  background: var(--af-canvas);
}

button, input, textarea, select { font: inherit; color: inherit; }
button { cursor: pointer; }

/* 会被纵向比较的数字统一走这个类（§3.4） */
.af-num {
  font-family: var(--af-font-mono);
  font-variant-numeric: tabular-nums;
}

/* 焦点环（§3.6）—— 无障碍要求，禁止 outline:none 而不给替代 */
:focus-visible {
  outline: 2px solid var(--af-accent);
  outline-offset: 1px;
}
```

### 4.4 `src/theme/ThemeProvider.tsx`（双主题同源）

> **这是 DESIGN §9 的一条硬要求**：主题状态必须**同时驱动**两样东西 ——
> ① AntD 的 `<ConfigProvider theme={...}>`（管组件）② `<html data-theme="...">`（管自绘区与 CSS 变量）。
> **两者必须同源同步**，否则会出现"AntD 组件是深色、自绘区域还是浅色"的错位。

```tsx
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { ConfigProvider } from 'antd';
import { lightTheme, darkTheme } from './antdTheme';
import type { ThemeMode } from './palette';

interface Ctx { mode: ThemeMode; toggle: () => void; }
const ThemeCtx = createContext<Ctx>({ mode: 'light', toggle: () => {} });
export const useTheme = () => useContext(ThemeCtx);

const STORAGE_KEY = 'af-theme';

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem(STORAGE_KEY) as ThemeMode | null;
    if (saved === 'light' || saved === 'dark') return saved;
    // 没有显式选择过 → 跟随系统偏好。注意系统偏好只是**初值**，
    // 用户手动切过之后以 localStorage 为准。
    return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });

  // ★ 唯一同步点：mode 变了，同时改 DOM 属性 + 存盘
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', mode);
    localStorage.setItem(STORAGE_KEY, mode);
  }, [mode]);

  const value = useMemo(
    () => ({ mode, toggle: () => setMode((m) => (m === 'dark' ? 'light' : 'dark')) }),
    [mode],
  );

  return (
    <ThemeCtx.Provider value={value}>
      <ConfigProvider theme={mode === 'dark' ? darkTheme : lightTheme}>
        {children}
      </ConfigProvider>
    </ThemeCtx.Provider>
  );
}
```

> **⚠ 禁止在组件里写局部 `<ConfigProvider>` 覆盖。** 唯一入口就是这里。
> 某个组件确实需要独有样式时，在 §4.2 的 `components` 字段里加，不要散落各处。

> **可选项（先别急着用）**：AntD v5 支持 `theme={{ cssVar: true }}`，开启后它会把**自己算好的** token 输出成 CSS 变量（形如 `--ant-color-primary`），
> 好处是彻底消灭"色板两处落地"的问题（自绘区和图表直接读 `--ant-*`）。
> ⚠ **但变量名的确切格式没有实跑验证过**，属于推测。若你想用，**先在本机开 DevTools 看 `<html>` 上的 `--ant-*` 实际命名**，确认后再决定。

### 4.5 v0 类名 → AntD 组件 映射表

> 这是"方案①：只取视觉"的核心工作。**v0 的 221 行 CSS 里，约 2/3 会被 AntD 组件取代，剩下 1/3 原样搬进 CSS Modules。**

| v0 的类名 | 落地方式 | 备注 |
|---|---|---|
| `.app-shell` / `.sidebar` / `.topbar` / `.main-area` / `.page-content` | AntD `<Layout>` / `<Layout.Sider>` / `<Layout.Header>` / `<Layout.Content>` | token 已在 §4.2 配好 |
| `.primary-button` / `.secondary-button` | `<Button type="primary">` / `<Button>` | 高度 32 由 `controlHeight` 保证 |
| `.icon-button` | `<Button type="text" shape="square" />` | — |
| `.model-select` / `.small-select` | `<Select size="small" />` | 宽度自行约束（v0: 140px / 112px） |
| `table` / `th` / `td` / `.table-card` | `<Table>` | ★ 行高由 §4.2 的 `cellPaddingBlock: 6` 决定；**不要照抄 v0 的 `height:44px`** |
| `.compact-table` | `<Table size="small">` 或 `size="middle"` | — |
| `.tool-call`（工具调用卡） | `<Collapse ghost>` | **ghost = 无边框折叠面板**；DESIGN §8.2 点名用它 |
| `.citation-drawer` / `.drawer-backdrop` | `<Drawer>` | 宽 420px、`placement="right"`、mask 自带 |
| `.run-card` / `.metric-card` | `<Card>` + 自绘内部 | `<Statistic>` **气质偏"中后台"，可以不用** |
| `.status-dot` | **自绘** | `<Badge status>` 的形状不会长成 8px 圆点，别硬套 |
| `.failure-bar` / `.rank-bar` / `.progress-line` | **自绘** | `<Progress>` 是带文字的横条，形状不对 |
| `.fake-chart`（手绘 SVG 假图） | **ECharts** | 见 §5.8；`fake-chart` 这个类名是 v0 自己承认的 mock |
| `.search-box` | `<Input prefix={<SearchOutlined />} allowClear />` | — |
| `.filter-pill` | `<Button>` 或 `<Segmented>` | v0 的形态是"带计数的 pill" |
| `.tag`（来源标签） | `<Tag>` | 需要覆盖 `borderRadius: 3px` |
| `.footnote` / `.table-note` | 普通 `<p>` | — |
| 空态 | `<Empty>`（**重写 `description`，不用默认插画**） | R09 |
| 加载态 | `<Skeleton active>` + 形状对齐真实内容 | R09 |
| 全屏遮罩层 | `<Modal>` / `<Drawer>` 自带 | — |

**AntD 覆盖不到的边界（诚实交代，别指望 token 万能）**：

1. AntD 组件**内部结构**的间距 —— 部分有组件 token，部分只能靠 CSS Modules 覆盖（比如 `Form.Item` 默认 `margin-bottom: 24px`）。
2. `Table` 在 `virtual`（虚拟滚动）模式下，行高由 `scroll.y` 参与计算，**token 影响有限**，要单独调。
3. `Descriptions`、`Statistic`、`Empty` 的默认插画视觉气质偏"中后台"，**token 改不动，要么少用、要么整个替换掉**。
4. → **自查方法**：改完 token 先看一遍所有用到的组件，把"改了 token 还是不对"的列出来，逐个决定是加 `components` 配置还是 CSS 覆盖。

---

## 5. 页面规格（逐页）

> **本章的写法**：每个页面分四段 —— **① 结构**（DOM 层次）、**② 关键尺寸**（从 v0 的 221 行 CSS 里实测提取）、**③ 数据源**（接哪个接口、字段怎么映射）、**④ 缺口**（哪些字段后端没有）。
> 尺寸值可以直接用；**标 ⚠ 的地方是要改的**。
>
> 页面顺序 = 导航顺序。

### 5.1 应用外壳（AppShell + Sidebar + Topbar）

#### 结构

```
<Layout>（.app-shell：display flex, height 100vh, bg canvas）
├── <Layout.Sider>（.sidebar）
│   ├── .brand-row        品牌行：.brand-mark（Bot 图标）+ "AgentForge" + 折叠按钮
│   ├── .nav-section-label  "工作台"（11px 小标题）
│   ├── <nav>.main-nav     7 个 .nav-item（图标 + 文字 + 可选 .nav-count）
│   ├── .history           最近会话：.history-header + N 个 .session
│   └── .sidebar-bottom    .nav-item（设置）+ .user-row（.avatar + 姓名/团队）
├── <Layout>（.main-area：flex column）
│   ├── <Layout.Header>（.topbar）
│   │   ├── .topbar-left   PanelLeft 图标 + 当前页标题 + "/" + "production"
│   │   └── .topbar-right  .api-status（绿点 + "API 正常"）+ <Select> 模型 + 主题切换 + 帮助
│   └── <Layout.Content>（.page-content）← 7 页在这里切换
└── <Drawer>（引用抽屉，全局唯一，见 §5.9）
```

#### 关键尺寸

| 对象 | 值 | 备注 |
|---|---|---|
| `.sidebar` 宽 | **216px** / 折叠 **56px** | `transition: width .18s ease` |
| `.sidebar` padding | `12px 8px` | — |
| `.brand-row` | h **32px**，gap 8px，字号 15px / 500 | — |
| `.brand-mark` | **23×23**，1px `accent` 描边，`accent` 文字色，圆角 4px | 里面放 `<Bot>` 图标，size 15 |
| `.nav-section-label` | 11px，`text-tertiary`，padding `20px 9px 6px` | — |
| `.nav-item` | h **32px**，gap 10px，padding `0 9px`，圆角 4px | 默认 `text-secondary` |
| `.nav-item:hover` | bg `sunken`，文字 `text-primary` | — |
| `.nav-item.active` | 文字 `accent`，bg **`accent-soft`**，字重 500 | ★ 这是 accent 的合法用途之一 |
| `.history` | margin-top **25px** | — |
| `.history-header` | 11px `text-tertiary`，padding `0 9px 7px`，右侧 `<MoreHorizontal>` 14 | — |
| `.session` | padding `5px 8px`，**左边框 2px 透明**，圆角 `0 4px 4px 0` | 标题单行省略号，时间 10px |
| `.session.active` | 左边框 2px **`accent`**，bg `sunken`，文字 `text-primary` | — |
| `.user-row` | h **36px**，上边框，padding `10px 8px 0` | — |
| `.avatar` | **24×24**，bg `sunken`，1px `border-strong` 描边，圆角 4px，字号 10 | — |
| `.topbar` | h **48px** | ✅ 与 §4.2 的 `headerHeight: 48` 一致 |
| `.topbar` padding | `0 16px` | — |
| `.topbar-left/right` | gap 10px | — |
| `.api-status` | gap 6px，11px `text-secondary`；圆点 **7×7** `success` | — |
| `.model-select` | h **28px**，w **140px**，1px `border-strong`，圆角 4px | — |
| `.page-content` padding | **`20px 24px`** | — |

#### 数据源

| 元素 | 接口 | 字段 |
|---|---|---|
| `.api-status` | `GET /api/health` | `status === 'ok'` → 绿点 + "API 正常"；`'degraded'` → 琥珀 + "依赖异常"；请求失败 → 红 + "连接失败" |
| `.history`（最近会话） | `GET /api/sessions?limit=20` | `[{id, title, updated_at}]` |
| 模型下拉 | **无接口**。后端的模型由 `.env` 决定，不是运行时可选 | ⚠ 见缺口 |

#### 缺口

1. **"模型选择"下拉没有后端支持。** 后端的 LLM 通道由 `.env` 配置（`deepseek` / `openai`），`POST /api/chat` **不接受模型参数**。
   → **建议**：M6 期间把它做成**只读展示**（显示当前配置的模型名，不可切），或直接去掉。**不要**让它看起来能选却什么也不做。
2. `.user-row`（"Lin Wei / 工程团队"）是 v0 的假人。本项目**没有用户系统**。→ 建议换成**当前环境标识**（如 `production` / `local`）或整个删掉。
3. `.nav-count`（"3"）在 v0 里是硬编码。真实可用的是**未读/进行中**语义，但**没有接口**。→ 建议去掉，或改成"文档处理中数量"（可从 `/api/documents` 统计）。

---

### 5.2 P1 · 对话台 ChatPage

> **这是最重的一页**，也是唯一有"过程可视化"的页面。

#### 结构

```
.chat-page（height: calc(100vh - 88px)，max-width 1080px，居中，1px 描边，bg surface）
├── .chat-toolbar（h 48px，padding 0 14px，下边框）
│   ├── 左：<strong>会话标题</strong> + <span class="mono muted">会话 ID</span>
│   └── 右 .toolbar-actions：secondary「新建会话」+ secondary「导出」
├── .message-list（flex:1，overflow auto，padding 16px 20px）
│   ├── .message.user-message          ← 用户：bg sunken + 左边框 2px
│   │   ├── .message-label "你"
│   │   ├── <p>消息正文</p>
│   │   └── .message-meta 时间
│   └── .message.agent-message         ← Agent：上下分隔线，无气泡
│       ├── .message-label "Agent" + <span class="mono muted">模型名</span>
│       ├── .tool-call × N             ← 工具调用卡（可折叠）
│       ├── <p>正文 + .citation-link "[1]"</p>
│       └── .message-meta.mono 耗时 · token · 成本
├── .composer（padding 10px 12px，上边框）
│   ├── .composer-tools：回形针按钮 + "Enter 发送 · Shift+Enter 换行"
│   ├── <textarea>（bg sunken，1px 描边，圆角 4px，resize none）
│   └── .composer-bottom：左"上下文：知识库 · N 份文档" + 右 primary「发送」
└── .chat-status（h 28px，上边框，11px tertiary）会话统计 + 绿点
```

#### 关键尺寸

| 对象 | 值 |
|---|---|
| `.chat-page` | `height: calc(100vh - 88px)`，`max-width: 1080px`，`margin: 0 auto` |
| `.chat-toolbar` | h **48px**，padding `0 14px` |
| `.message-list` | padding `16px 20px` |
| `.message` | padding `11px 12px`，margin-bottom 10px；正文 `line-height: 21px` |
| `.user-message` | bg `sunken`，**左边框 2px `border-default`**，圆角 4px |
| `.agent-message` | **只有上下 1px 分隔线，无背景、无圆角** ← ★ 关键视觉特征 |
| `.message-label` | 11px `text-secondary`，gap 8px |
| `.message-meta` | 11px `text-tertiary` |
| `.citation-link` | `accent` 色，**虚线下划线**（`text-decoration-style: dotted`） |
| `.composer textarea` | bg `sunken`，padding `9px 10px`，`line-height: 20px`；focus 时描边变 `accent` |
| `.chat-status` | h 28px，11px `text-tertiary`，上边框，padding `0 14px` |

> ★ **两条不能丢的视觉特征**（DESIGN §8.2）：
> - **消息块之间用分隔线，不用气泡尾巴** —— Agent 消息就是上下两条线，没有圆角背景块。
> - **用户消息与 Agent 消息靠「左对齐 + 背景微差」区分，不靠颜色。** 用户消息有个浅灰底（`sunken`）+ 左侧 2px 竖条；Agent 消息是纯白底 + 上下线。

#### ToolCallCard（工具调用卡）规格

```
.tool-call（1px 描边，圆角 4px，margin 9px 0 10px，bg surface）
├── .tool-summary（h 28px，gap 8px，padding 0 8px，字号 11px）
│   ├── .status-dot.success/failed     8px 色点
│   ├── <span class="mono">工具名</span>
│   ├── .tag                          来源标签（local / Harness / inventory）
│   ├── .tool-time.mono.muted          耗时（右对齐：margin-left auto）
│   └── <ChevronDown>                  展开箭头
└── .tool-detail（展开后：grid 54px 1fr，gap 5px 8px，上边框，padding 8px）
    ├── .detail-label "入参"      → <code>{...}</code>
    └── .detail-label "返回摘要"  → <code>...</code>
```

**落地方式**：用 AntD `<Collapse ghost>`（ghost = 无边框折叠面板）。
`.tag` 用 AntD `<Tag>` 但需覆盖 `borderRadius: 3px`；`.status-dot` **必须自绘**（`<Badge>` 的形状不对）。

#### 数据源

**主接口**：`POST /api/chat`

```jsonc
// 请求
{ "message": "这个项目为什么不用 LlamaIndex？", "session_id": "可选的 UUID" }

// 响应
{
  "session_id": "a3f2c1d8-...",              // 不传则服务端新建，前端存下来下一轮回传
  "answer": "主要是因为控制权。[1] 项目把检索链路…",
  "sources": [                                // ★ 与 answer 里的 [n] 一一对应
    { "index": 1, "source": "技术选型说明.md", "content": "本项目检索层不引入…",
      "similarity": 0.83, "page_ref": "p.3" }
  ],
  "invalid_citations": [5],                   // 回答里引用了但来源表没有的编号（幻觉引用）
  "tool_calls": ["search_documents", "harness__list_pipelines"]   // 本轮调用的工具名序列
}
```

**字段映射**：

| 界面元素 | 来源 |
|---|---|
| 消息正文 + `[n]` 高亮 | `answer`（用正则 `/\[(\d+)\]/g` 切成片段，命中 `sources[].index` 的渲染成按钮） |
| 引用抽屉 | `sources[]` → `index` / `source`（文件名）/ `content`（原文）/ `similarity`（余弦相似度）/ `page_ref`（页码，可空） |
| 幻觉引用降级 | `invalid_citations`，命中的 `[n]` 渲染成**灰色不可点**（并在 title 里写"无此来源"） |
| ToolCallCard 工具名 | `tool_calls[]` |
| ToolCallCard 的 `.tag` 来源 | 用工具名去 `GET /api/tools` 的 `by_source` 反查（`local` / `harness` / `inventory`） |
| 会话历史侧栏 | `GET /api/sessions` |
| 载入某会话的历史 | `GET /api/sessions/{id}/messages` |

#### ★ 缺口（**三个，必须按这里处理**）

| # | 缺什么 | 处理方式 |
|---|---|---|
| **G1** | **后端目前是非流式**（`/api/chat` 返回完整 JSON，**没有 SSE 端点**）<br>（**SSE** = Server-Sent Events，服务器**单向持续推流**的技术，前端用它做"打字机"逐字显示） | 见下方 §5.2.1 |
| **G2** | **消息尾部的"耗时 · token · 成本"没有数据源**。`/api/chat` 的响应里**没有**这三个字段 | **先留空 / 不渲染这一段**，或用 mock 并标注。**不要去"算"一个假的**。这些数据在后端 Langfuse 里（D28 建的通道），但**目前没有 REST 接口暴露** |
| **G3** | **工具卡片没有"耗时"**。`/api/chat` 只给工具名 | 隐藏耗时那一格。**入参与返回摘要**可以从 messages 接口补（见下） |

**关于 G3 的补法**（可选，做了更好看）：

`GET /api/sessions/{session_id}/messages` 返回的 `role: "tool"` 与带 `tool_calls` 的 `role: "assistant"` 消息里，**有真实的入参与返回**：

```jsonc
[
  { "role": "user", "content": "...", "tool_calls": null, "created_at": "..." },
  { "role": "assistant", "content": "", "tool_calls": [
      { "id": "call_1", "type": "function",
        "function": { "name": "search_documents", "arguments": "{\"query\":\"…\",\"top_k\":5}" } }
    ], "created_at": "..." },
  { "role": "tool", "content": "命中 5 个片段，最高相似度 0.83", "tool_calls": null, "created_at": "..." },
  { "role": "assistant", "content": "主要是因为控制权。[1] …", "tool_calls": null, "created_at": "..." }
]
```

→ **入参** = `tool_calls[].function.arguments`（是个 **JSON 字符串**，要 `JSON.parse`）
→ **返回摘要** = 紧随其后那条 `role: "tool"` 消息的 `content`
→ **成功/失败** = `content` 里含 `"执行失败"` 或 `"未知工具"` 即为失败（后端的约定，见 `agent_service.py`）

⚠ **注意**：这是一次**额外请求**（消息接口是按会话全量拉），不要在每条消息渲染时都调。建议**进入会话时拉一次**，然后在内存里把 tool 调用配对。

#### 5.2.1 SSE 流式（**G1 的处理方案**）

> **注解 · SSE（Server-Sent Events）**：服务器**单向、持续**往浏览器推数据的技术。前端拿它做"打字机"效果——不用等整段回答生成完，而是逐字显示。

**现状**：后端的代码注释里写得很清楚——

> 「PRD 规划的是 SSE 流式（逐字返回）… 项目现在还没有前端，先用非流式 JSON 把「Agent 服务层 → HTTP 出口」这段接通…
> 将来上流式时，前端本来就要把 fetch 改写成 EventSource，所以现在用 JSON 不构成额外的技术债。」

**所以：M6 期间后端不会有 SSE。** 你有两个选择：

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | **界面按 v0 稿做好**（`.chat-status` 有状态区、消息有 meta 行），发送时调 `/api/chat`，**期间显示"思考中"骨架**，返回后**一次性渲染**整段回答 | 不引入假动画，诚实反映"这是非流式" |
| **B** | 前端**模拟**逐字输出（把完整 answer 切帧渲染） | ⚠ 会让人以为后端在流式。**如果做，必须在界面上标注"模拟流式"** |

**无论选哪个，请把 `src/api/chat.ts` 写成"将来换成 SSE 时改动最小"的形状**：

```ts
// 现在：一次性返回
export async function sendMessage(message: string, sessionId?: string) {
  const { data } = await client.post<ChatResponse>('/chat', { message, session_id: sessionId });
  return data;
}

// 将来后端上 SSE 时，只需要在这里加一个 sendMessageStream()，
// 因为界面消费的是"最终的消息对象"，不是"HTTP 响应的形状"
```

#### 交互清单

- [ ] 发送消息：Enter 发送，**Shift+Enter 换行**（`onKeyDown` 里判断）
- [ ] 首轮不传 `session_id`；拿到响应后**存下 `session_id`**，后续轮次回传
- [ ] 点 `[n]` → 打开引用抽屉，展示对应 `sources[n]`
- [ ] 点 `invalid_citations` 里的编号 → 不打开抽屉，提示"该引用没有对应来源"
- [ ] 「新建会话」→ 清空当前 `session_id` 与消息列表
- [ ] 侧栏点历史会话 → 载入该会话的 `messages`
- [ ] 消息列表**自动滚到底部**（新消息进来时）

---

### 5.3 P2 · 知识库 KnowledgePage

#### 结构

```
<PageHeader title="知识库" description="管理可检索文档与索引处理状态。">
  action: primary「上传文档」（Upload 图标）
.stat-strip（统计条：8 份文档 · 7 已就绪 · 1 处理中 · 412 个切片）
.table-card
├── .table-toolbar：.search-box（"搜索文件名"）+ secondary「全部状态」
└── <table>
    thead: 文件名 | 类型 | 状态 | 切片数(右对齐) | 上传时间 | （操作）
    tbody: N 行
      └── .file-cell：.file-icon（按类型着色）+ 文件名
            ├── processing → .progress-line（2px 流动进度条）
            └── failed     → <small class="error-text">失败原因</small>
          类型 | .state（色点+文字）| 切片数 | 时间 | .row-action（重试/删除）
.footnote（"共 N 份文档 · 已就绪 N · 处理中 N · 共 N 个切片"）
```

#### 关键尺寸

| 对象 | 值 |
|---|---|
| `.stat-strip` | padding `9px 12px`，圆角 6px，gap 7px，12px；数字用 **mono 14px `text-primary`**；分隔用 1px × 14px 竖线 |
| `.table-toolbar` | padding `10px 12px`，下边框 |
| `.search-box` | h 28px，w **240px**，1px `border-strong`，圆角 4px |
| `th` | h **32px**，bg `sunken`，11px / 500，`text-tertiary` |
| `td` | ⚠ **删除 `height: 44px`**（见 §3.5），padding `6px 12px` |
| `.file-icon` | **24×24**，1px 描边，圆角 4px；`.pdf` → `danger`，`.docx` → `info`，其他 → `text-secondary` |
| `.progress-line` | w 230px，h **2px**；内条 35% 宽 `accent` + 流动动画 |
| `.row-action` | 无边框，`danger` 色，11px |

> ⚠ **`.progress-line` 的动画是 R07 的合法例外**（"唯一例外：流式输出的光标与进度条"）。
> 它的 `@keyframes` 是：`0% { translateX(-100%) } 100% { transform: translateX(320%) }`，`1.2s infinite ease-in-out`。

#### 数据源

| 界面元素 | 接口 | 字段 |
|---|---|---|
| 文档表格 | `GET /api/documents?limit=50&offset=0` | `[{id, filename, file_type, status, chunk_count, error_message, created_at}]` |
| 上传 | `POST /api/documents`（**multipart**） | 返回 **202** + 单个 `DocumentOut` |
| 删除 | `DELETE /api/documents/{id}` | **204 No Content**（无响应体） |
| 状态色点 | `status` 字段 | `processing` → 琥珀；`ready` → 绿；`failed` → 红 |
| 失败原因 | `error_message` | `status === 'failed'` 时才有值 |
| `.stat-strip` 的统计 | **前端自己算**（把列表 reduce 一遍） | 文档数 / ready 数 / processing 数 / 切片数求和 |

**轮询**：上传后文档处于 `processing`，需要**轮询** `GET /api/documents` 直到状态变成 `ready` / `failed`。
建议：**有任一文档处于 `processing` 时才开轮询**（间隔 2s），全为终态就停。别一直轮。

#### 交互清单

- [ ] 上传：`<Upload>` 或原生 `<input type="file">` → `FormData` POST
- [ ] 上传后**立刻插入一行**（状态 processing），并启动轮询
- [ ] 删除要二次确认（`<Popconfirm>`）
- [ ] 搜索框**前端过滤**（后端没有搜索参数）
- [ ] 「全部状态」筛选：默认按钮形态，可做成 `<Select>`（前端过滤）

#### 缺口

1. **没有"重试"接口**。v0 的 `.row-action` 在 failed 时会显示"重试"，但后端**只有删除**。→ **failed 行只显示"删除"**，或把"重试"做成"删除后重新上传"。
2. **没有分页 UI 需求**：`limit` 默认 50，够用。若文档超 50 份需要翻页，再议。

---

### 5.4 P3 · 评测看板 EvalPage

> **这是数据源最完整的一页**（后端 7 个评测接口全是给它准备的），也是**面试演示的门面**。
> 但有一件事必须先说清楚：**界面上的数字必须来自接口，不能照抄 v0 稿里的 mock 数字**（见 §5.4.3）。

#### 结构

```
<PageHeader title="评测看板" description="比较检索配置，定位回答质量与失败模式。">
  action: <Select> 三配置 + primary「运行新评测」
.run-row（3 列网格：三配置对比卡）
  └── .run-card（.selected 时有 accent 描边 + accent-soft 底）
      ├── .run-card-top：<span class="mono">配置名</span> + <strong>准确率</strong>（20px mono）
      ├── <span class="muted mono">时间</span>
      ├── .fingerprints：题集指纹 + 语料指纹（各取前 8 位 + "…"）
      └── <small class="muted">judge 模型 · N 次生成</small>
.metric-grid（4 列：准确率 / 正确性均分 / 引用忠实度 / 完整性）
  └── .metric-card：11px 标签 + 24px mono 数值 + 10px 说明
.split-grid（1.5fr : 1fr）
├── .panel 分类聚合（.compact-table：类别 | 题数 | 明细行数 | 通过行数 | 三维分数）
│   └── .table-note（说明工具题为何是 —）
└── .panel 失败模式（.failure-list：5 行，每行 = 名称 + 5px 横条 + 计数）
.panel.cases-panel 逐题明细（.compact-table + .truncate-cell）
    thead: 题号 | 类别 | 答案摘要 | 引用数 | 三维分数 | 结果 | 失败原因
```

#### 关键尺寸

| 对象 | 值 |
|---|---|
| `.run-row` | grid 3 列，gap 10px |
| `.run-card` | padding 12px，圆角 6px，1px 描边；`.selected` → 描边 `accent` + 底 `accent-soft` |
| `.run-card-top strong` | **500 20px mono** |
| `.fingerprints` | 10px，gap 15px，`text-tertiary`；指纹值用 mono |
| `.metric-grid` | grid 4 列，gap 10px |
| `.metric-card` | padding `12px 14px`，圆角 6px；数值 **500 24px/30px mono** |
| `.split-grid` | grid `1.5fr 1fr`，gap 16px |
| `.panel-title` | padding 12px，下边框；`h2` 13px/500 |
| `.compact-table th` | h **30px** |
| `.compact-table td` | h **40px** → ⚠ 删掉 `height`，让它自然落到 32px（§3.5） |
| `.failure-row` | grid `80px 1fr 25px`，h 30px，11px |
| `.failure-bar` | h **5px**，bg `sunken`，圆角 2px；内条 `danger`，`opacity: .75` |
| `.truncate-cell` | `max-width: 280px`，单行省略号 |
| 低分高亮 | 分数 < 4 时用 `warning` 色（v0 的 `.amber-text`） |

> ⚠ **`.compact-table td` 的 `height: 40px` 同样要删**（理由见 §3.5）。

#### 5.4.1 数据源

**主接口**（全部只读）：

| 用途 | 接口 | 关键字段 |
|---|---|---|
| 三配置对比卡 | `GET /api/eval/runs?limit=50` | 见下方"取最新一轮" |
| 汇总 + 分类聚合 + 失败分布 | `GET /api/eval/runs/{run_id}` | `by_category[]` / `failure_breakdown` / `rows` / `scored_rows` / `generation_inconsistent` |
| 逐题明细 | `GET /api/eval/runs/{run_id}/cases?category=&passed=` | `[{case_key, category, generation_index, answer, tool_calls, sources_count, score_*, judge_runs, passed, failure_reason, failure_reason_label}]` |
| 某题下钻 | `GET /api/eval/runs/{run_id}/results/{case_key}` | 追加 `retrieved_chunks`（字符串）与 `judge_raw` |
| 评测集概览 | `GET /api/eval/dataset` | `{total, negative, by_category, by_difficulty, covered_slugs, fingerprint}` |
| 题目列表 / 详情 | `GET /api/eval/cases` / `/cases/{case_key}` | 列表**不含答案**；详情含 `reference` / `evidence` / `expected_tool` |

**★ "取每个配置的最新一轮"怎么算**：

`GET /api/eval/runs` 返回的列表**最新在前**。所以：

```ts
// 三配置 = pure_vector / hybrid / hybrid_rerank
const latestByConfig = new Map<string, EvalRun>();
for (const run of runs) {                       // runs 已按 id 倒序
  if (!latestByConfig.has(run.config_name)) latestByConfig.set(run.config_name, run);
}
```

（后端 `list_runs` 的 SQL 等价于 `distinct on (config_name) … order by id desc`，前端这里做一次等价过滤即可。）

#### ★ 5.4.2 三个必须显示在界面上的口径（DESIGN §8.3 的硬要求）

**"口径必须写在 UI 上，不能只留在文档里。"** 这是 DESIGN 对评测页最重的一条要求。

1. **`scored_rows` 与 `rows` 是两个不同的分母，且不可互证。**
   - `accuracy` 分母是**题数**（每题多次生成先多数投票）
   - `score_*` 均分分母是 **`scored_rows`（有分数的明细行数）**
   - → UI 上**必须写清**："按题计算，每题多次生成先取多数结论" / "按行计算，分母是 N 行（有分数的行）"
   - → **不要**写成"共 N 行"，那是 `rows`，不是 `scored_rows`。**这个错极易犯，而且数字本身没错、只是回答的不是那个问题。**

2. **工具题显式标注"只看工具调用，不计分"。**
   工具类题（`expected_tool` 非空）**不送 judge 判内容**，所以三维分数是 `NULL`，界面上显示 `—`。
   表格下方用 `.table-note` 写明原因，否则用户会疑惑为什么它是空。

3. **失败模式用 `failure_reason_label`，不要自己拼中文。**
   后端已经给了中文标签（在 schema 层算的，避免与枚举定义漂移）。直接用。

#### ★ 5.4.3 mock 数字纪律（**必读**）

v0 稿里的评测页数字**全部是编的**，与真实后端**完全不同**：

| v0 稿写的 | 真实情况（`docs/eval-report-2026-09-30.md` 实测） |
|---|---|
| 准确率 `62.5%` / `58.3%` / `50.0%` | 三配置**都**是 `98.00%（49/50）` |
| "分母是 **48** 行" | 真实分母是 **40 行**（共 50 行） |
| "当前 60 条，分四类：文档问答 30 / 跨文档 10 / 工具调用 10 / **多步分析 10**" | 当前是 **50 条**（30 + 10 + 10）。**"多步分析"这一类还没建**（它是后续里程碑才加的） |
| 失败分布 12 / 8 / 5 / 3 / 2（共 30 条） | 三配置合计**只有 3 条判定失败，涉及 1 道题**（`B01`） |
| 题集指纹 `32d4d63c` / 语料 `a7797b06` | 题集 `a7797b06…90afe` / 语料 `5783a666…bbdcc9` |

> **红线**：**这些数字一个都不要照抄进界面**。全部从接口读。
> 没有数据时（比如没跑过评测）就走**空态**（§3.7），不要显示编的假数据。

#### 5.4.4 缺口

| # | 缺什么 | 处理 |
|---|---|---|
| **E1** | **没有"触发评测"的接口**。评测是通过命令行跑的（`uv run python -m scripts.eval_run`） | → `「运行新评测」` 按钮 M6 期间应**禁用并加 tooltip**："评测暂由命令行触发（`scripts/eval_run.py`）"；或做成"显示该命令 + 复制到剪贴板"。**不要**让它看起来能点却什么都不做 |
| **E2** | 三配置下拉可能**取不满三个**（若某个跑过但被标灰/作废） | 按实际返回渲染，缺哪个就不显示哪个卡片 |
| **E3** | 逐题明细的"三维分数"在 `cases` 接口里是**三个独立字段** | 自己拼成 `5 / 4 / 5` 形式；全为 `null` 时显示 `—` |

#### 交互清单

- [ ] 点某张 run 卡 → 选中（`.selected`），下方所有面板切到该 `run_id` 的数据
- [ ] 逐题明细的两个下拉（类别 / 结果）→ 对应接口的 `category` / `passed` 参数
- [ ] 点某一行题 → 可展开或跳转到该题的多次生成对比（`/results/{case_key}`）
- [ ] 全部数据加载时走**骨架屏**（表格骨架画 5 行格子），不要转圈

---

### 5.5 P4 · 工具注册表 ToolsPage

#### 结构

```
<PageHeader title="工具注册表" description="已注册的 MCP 与本地工具，共 N 个。">
  action: primary「注册工具」
.tool-filters
├── .search-box（"搜索工具名称或描述"）
└── .filter-pill × 3：全部 (11) / MCP (7) / 本地 (4)   ← 数字来自 by_source 统计
.tool-grid（3 列）
  └── .tool-card
      ├── .tool-card-head：.tool-icon（28×28）+ .state（色点 + "可用" / "需授权"）
      ├── <strong class="mono">工具名</strong>
      ├── <p>描述</p>
      └── .tool-card-foot：.tag 类型 + mono muted "平均 xxx" + icon-button（设置）
```

#### 关键尺寸

| 对象 | 值 |
|---|---|
| `.tool-filters` | flex，gap 8px，margin-bottom 14px |
| `.filter-pill` | h 28px，1px 描边，圆角 4px，padding `0 10px`，11px；`.active` → 描边+文字 `accent`，底 `accent-soft` |
| `.tool-grid` | grid 3 列，gap 10px |
| `.tool-card` | padding 13px，圆角 6px |
| `.tool-icon` | **28×28**，1px 描边，圆角 4px，图标色 `accent` |
| `.tool-card p` | h **35px**（固定，让卡片等高），11px，`line-height: 17px` |
| `.tool-card-foot` | 上边框，padding-top 9px，gap 8px |

#### 数据源

**唯一接口**：`GET /api/tools`

```jsonc
{
  "total": 13,
  "by_source": { "local": ["current_time", "search_documents"], "inventory": [...], "harness": [...] },
  "servers":  [ /* 见下方两种形态 */ ],
  "tools":    [ { "name": "search_documents", "description": "搜索知识库中的相关片段",
                  "parameters": { /* JSON Schema，来自函数签名 */ }, "source": "local" } ]
}
```

**`servers[]` 有两种形态（已核对源码 `app/mcp/manager.py`）**：

```jsonc
// ① 连接成功
{ "name": "harness", "connected": true, "tools": ["…11 个工具名…"],
  "tool_count": 11, "server_info": { "name": "…", "version": "…" },
  "protocol_version": "…", "stderr_log": null, "error": null }

// ② 连接失败
{ "name": "harness", "connected": false, "tools": [],
  "error": "人话解释（为什么没连上）", "stderr_log": "/path/to/stderr.log" }
```

> ⚠ **坑**：**失败形态里没有 `tool_count` 字段**。前端读 `servers[].tool_count` 时，失败项会拿到 `undefined`。
> → 一律用 `srv.connected ? srv.tool_count : 0`，或用 `srv.tools?.length ?? 0`。
> TS 类型上把 `tool_count` 声明成可选（`tool_count?: number`），别写成必填——写了必填 TS 会编译过但运行时是 `undefined`。

**映射**：

| 界面元素 | 字段 |
|---|---|
| 卡片列表 | `tools[]` |
| 工具名 | `name`（**`harness__` 前缀是服务端加的命名空间，原样显示**） |
| 描述 | `description` |
| `.tag` 类型 | `source`（`local` → "本地"；其余显示 server 名） |
| 筛选 pill 的计数 | `by_source` 的各数组长度 + `total` |
| 展开详情的参数表 | `parameters`（是 JSON Schema，用 mono 字体渲染） |
| Server 连接状态 | `servers[].connected`（**这是与 `tools[]` 不同的信息**：工具还在表里但 server 断了 = 调不通） |
| 失败原因（排障用） | `servers[].error` + `stderr_log` 路径 |

#### ★ 缺口

| # | 缺什么 | 处理 |
|---|---|---|
| **T1** | **没有"平均耗时"**。v0 卡片底部写"平均 86ms"——**没有数据源** | 隐藏这一格 |
| **T2** | **没有"可用 / 需授权"状态**。`tools[]` 里没有健康度字段 | 用 `servers[].connected` 做**近似**：工具所属 server 断了 → 显示 `warning` 色点 + "未连接"；本地工具恒为 `success` + "可用"。**名称改为"可用/未连接"，不要写"需授权"**（"授权"是本项目不存在的概念） |
| **T3** | **没有"注册工具"的接口** | `「注册工具」` 按钮应**禁用 + tooltip**："工具由 MCP server 在启动时动态发现，详见 `app/mcp/`" |
| **T4** | 没有调用记录 / 调用次数 | v0 稿没做这块，跳过 |

#### 交互清单

- [ ] 搜索框 → 前端过滤（名称 + 描述）
- [ ] pill 筛选 → 按 `source` 前端过滤
- [ ] 点卡片 → 展开显示 `parameters`（JSON Schema 格式化后用 mono 渲染）
- [ ] 空态：**写清"未连接 Server"**，不要显示一个空表（DESIGN §8.5）

---

### 5.6 P5 · Trace 时间线 TracePage（★ 追加页）

> ⚠ **这一页在后端完全没有接口。** 见下方"数据源"。
> 它在 DESIGN §8.1 里被称为"**招牌页，最先做**"——但那是**旧方案**的顺序（当时 v0 稿还没出，需要用最难的一页定调）。
> 现在调性已经被 v0 稿钉死了，**所以它排在 M6 的第 4 天**（见 §7.1）。这不影响它的面试价值。

#### 这页要表达什么

一次 Agent 运行（一轮对话）在 Langfuse 上是一条 **trace**，里面按父子关系挂着若干 **span**（检索、向量搜索、embedding、重排、每次工具调用、每次 LLM 调用）。
后端 D28 已经把埋点做全了：**一轮对话 = 一条 trace / 9 个 span / 唯一根 span**。

**这一页就是把那棵树画出来**——横条宽度 = 耗时占比。

#### 结构（**AntD 没有能用的组件，必须自绘**）

> `Timeline` 是竖向的，形状完全不对。自绘部分的色值走 §4.3 的 CSS 变量。

```
<PageHeader title="Trace 时间线">
.trace-toolbar：trace 选择器 + 时间范围 + 「刷新」
.trace-waterfall（自绘）
├── .trace-axis        顶部时间轴刻度（text-tertiary，密集但不抢戏）
└── .trace-row × N     每行 = 一个 span
    ├── .span-name     名称（按嵌套层级缩进）
    ├── .span-bar      横条（left% / width% 按时间比例，不是等宽）
    │                  ├─ 失败时：行首加 2px danger 左边条，色点用 danger，不铺红底
    │                  └─ hover 出浮层：起止时间 / 输入 / 输出 / token 数
    └── .span-duration 耗时（等宽，右对齐，.af-num）
```

#### 规格要点（DESIGN §8.1）

- **横向 span 图，按时间比例缩放**（不是等宽）—— 宽度差异本身就是信息
- **嵌套 span 用缩进 + 左侧竖线**表示父子，**不用括号**
- 每行：`名称 | 耗时（等宽右对齐）| 状态色点`
- hover 浮层：`surface-raised` + 描边 + §4.2 的单层阴影
- **失败 span 不铺红底**，只在状态点用 `danger` + 行首 2px 红条
- 密度守门线：**1440×900 首屏可见 ≥ 12 个 span**

#### ★ 数据源：**不存在**

| 需要的数据 | 现状 |
|---|---|
| trace 列表 | ❌ 无接口 |
| 单条 trace 的 span 树 | ❌ 无接口 |
| 每个 span 的耗时/输入/输出/token | ❌ 无接口。**数据在 Langfuse 里，但没有 REST 出口**（D28 建的 `langfuse_client.py` 目前只暴露给评测用，且只查 observations 聚合） |

**处理方式（按 §0.4 的红线）**：

1. **界面按上面的规格做完整**（自绘瀑布图 + hover 浮层 + 失败态）。
2. 数据来自 `src/mock/index.ts` 里一个**集中的 trace 结构**（§6.5），字段名照 §5.6 的语义命名。
3. 在那个 mock 的顶部写一行注释：`// TODO: 等后端暴露 trace 查询接口后替换`。

**M6 期间不要试图去直连 Langfuse API**——那会：
- 引入跨域问题（Langfuse 在 `:3000`）
- 而且要处理鉴权（PAT 在服务端 `.env` 里，**绝不能进前端**，这是项目红线 4「任何外部凭据不得进入代码」）

#### 面试话术提示（给你自己，不是给 AI）

这一页的价值在于**能演示"我知道 Agent 内部发生了什么"**。即使数据是 mock 的，**结构本身**（父子嵌套 / 时间比例 / 失败归因）就是可讲的东西。但**演示时必须说明数据来源**，不要让人以为是真实运行。

---

### 5.7 P6 · RAG 检索检查器 RetrieverPage（★ 追加页）

#### 这页要表达什么

一次检索的**取舍过程**：候选池 20 → RRF 融合 → 重排后取 5。**要看的是名次怎么变的、分数是多少、有没有命中正确答案。**

#### 结构

```
<PageHeader title="检索检查器">
.retriever-toolbar：题号 / 查询输入 + k 值选择（默认 5）+ 「检索」
.rank-compare（并排两列）
├── .rank-col 重排前（候选池）
│   └── .rank-item × 20：名次 + 文档名 + 分数 + 命中色点
└── .rank-col 重排后（最终 top-5）
    └── .rank-item × 5
.retriever-note（说明"分数是 logit，不是概率"）
```

#### 规格要点（DESIGN §8.4）

- 显示 **top-k 片段（默认 k=5）**，每条带来源文档名 + 名次
- **重排前后名次变化要并排显示**（候选池 20 → 最终 5，这是一次真正的"取舍"）
- **分数必须标注是 logit，不是概率** —— 这是最容易被误读的一个数
- 命中判定用**色点**（`success` = 是否命中 gold），**不用彩色整行**

#### ★ 数据源：**只有一段字符串**

唯一沾边的接口是 `GET /api/eval/runs/{run_id}/results/{case_key}` 里的：

```jsonc
{ "retrieved_chunks": "……（一段几千字的文本，格式化后的片段拼接）", "judge_raw": [...] }
```

**问题**：它是 `str`，不是结构化的片段列表。

| 这页要的字段 | 有没有 |
|---|---|
| 每条片段的内容 | ⚠ 在字符串里，需要解析（格式未定义，**不建议依赖解析**） |
| 名次 | ❌ |
| 重排前名次 | ❌ |
| 分数（logit） | ❌ |
| 是否命中 gold | ❌ |

**处理方式**：

1. **界面按规格做完整**，数据走 `src/mock/index.ts`。
2. 可选：先用 `retrieved_chunks` 的字符串**原样展示**（作为一个"检索片段原文"的文本块），让这一页至少有**一部分是真实数据**。**但不要去写正则解析它**——格式没有定下来，一改就崩。
3. mock 里字段的命名建议直接对齐后端将来可能的结构：`{rank_before, rank_after, doc, score_logit, is_gold}`。

---

### 5.8 P7 · 分析页 AnalyticsPage（**归属 D36，不在 M6 范围**）

> ⚠ **这一页的排期已经改到 D36**（与多步分析编排一起）。M6 期间可以**先不做**，或只搭骨架。
> 它出现在本规格里的原因：v0 稿里有这一页，且导航里有入口。**你至少要保证导航项不指向空白。**

#### 结构

```
<PageHeader title="分析" description="观察 Agent 的调用成本、延迟与运行稳定性。">
  action: secondary「导出报告」
.metric-grid.analytics-metrics（4 张卡：总请求 / 平均延迟 / Token 消耗 / 成功率）
.analytics-grid（1.6fr : 1fr）
├── .panel.chart-panel（min-height 300px）
│   ├── .panel-title + .legend（legend-line / legend-bar）
│   └── ★ .fake-chart → 换成 ECharts
└── .panel 热门工具
    └── .rank-list：.rank-item（序号 + 名称 + 3px 横条 + 次数）
```

#### 关键尺寸

| 对象 | 值 |
|---|---|
| `.analytics-grid` | grid `1.6fr 1fr`，gap 16px |
| `.chart-panel` | min-height 300px |
| `.rank-item` | grid `22px 1fr 40px`，padding `11px 0`，下边框 |
| `.rank-bar` | h **3px**，bg `sunken`；内条 `accent` |

#### ★ 两处必须改

1. **`.fake-chart` 要换成真图表。**
   v0 稿这里是**手绘的 SVG 假图**（类名就叫 `fake-chart`）：用 `repeating-linear-gradient` 画网格 + 内联 `<path>` 画曲线。
   → 按 PRD F9.7，这里用 **ECharts**。

2. **图表的双主题问题（最容易漏的一处）。**
   > **ECharts 不认 AntD 的 token。** 它需要你**显式把色值传进图表配置**。
   > 而且**切换主题时必须重建图表实例**（否则图表还停在旧主题的颜色上）。

   正确做法（封装在 `components/MetricChart.tsx` 里）：

   ```tsx
   const { mode } = useTheme();
   const p = palette[mode];          // ← 从 §4.1 的唯一真源读色，不要写死
   const option = {
     color: [p.accent],
     backgroundColor: 'transparent',  // 让容器决定背景
     xAxis: { axisLine: { lineStyle: { color: p.borderSubtle } },
              axisLabel: { color: p.textTertiary } },
     yAxis: { splitLine: { lineStyle: { color: p.borderSubtle } },
              axisLabel: { color: p.textTertiary } },
     // ...
   };
   // ★ key 里带上 mode，切主题时强制 React 重建实例
   return <ReactECharts key={mode} option={option} style={{ height: 220 }} />;
   ```

#### 数据源：**不存在**

需要 `GET /api/metrics`（语义层，D35 才做）。**当前后端没有这个端点。**
→ 按 §0.4 红线处理：界面 + mock，**不要编接口**。

---

### 5.9 引用抽屉 CitationDrawer（全局组件）

#### 结构

```
<Drawer placement="right" width={420}>（或自绘 .drawer-backdrop + .citation-drawer）
├── .drawer-head
│   ├── .eyebrow（11px mono，accent 色）"引用 [1]"
│   ├── <h2>检索来源</h2>（16px / 500）
│   └── 关闭按钮
├── .source-title：FileText 图标（accent）+ 文件名 + "知识库 · 已就绪"
├── .similarity：左"余弦相似度" + 右 <strong>（16px mono）
├── .source-copy（bg sunken，圆角 4px，padding 12px，line-height 22px）：原文片段
└── .secondary-button.full（宽 100%）："在知识库中打开" ↗
```

#### 关键尺寸

| 对象 | 值 |
|---|---|
| 抽屉宽 | **420px**（`<Drawer width={420}>`） |
| 遮罩 | v0 用 `#0b111c33`；用 AntD `<Drawer>` 时它自带 mask |
| 抽屉阴影 | `-10px 0 30px #00000012`（用 AntD 默认即可，或按需覆盖） |
| padding | 18px |
| `.source-copy` | bg `sunken`，`line-height: 22px` |

#### 数据源

直接来自 `POST /api/chat` 响应里的 `sources[index-1]`：

| 界面元素 | 字段 |
|---|---|
| 标题 | `source`（文件名 / 标题） |
| 相似度 | `similarity`（可为 `null`，此时隐藏这一行） |
| 原文 | `content` |
| 页码 | `page_ref`（如 `p.3`；**仅 PDF 有，其他格式为 null** → 有值时才显示） |

> 「在知识库中打开」按钮：**没有对应的深链接口**。→ 做成"关闭抽屉并切到知识库页"即可，**不要**假装能定位到具体文档。

---

## 6. 数据契约（真实后端接口）

> **本章的每一条都是从后端代码（`app/api/*.py` + `app/schemas/*.py`）逐个核对来的，不是编的。**
> 服务跑起来后，`http://localhost:8000/docs` 有自动生成的 OpenAPI 文档，可与本章对照。

### 6.1 端点总表（14 条路径 / 15 个端点）

| # | 方法 | 路径 | 用途 | 页面 |
|---|---|---|---|---|
| 1 | GET | `/api/health` | 健康检查（含依赖状态） | 外壳顶栏 |
| 2 | POST | `/api/chat` | 对话（**非流式**） | 对话台 |
| 3 | GET | `/api/documents` | 文档列表 | 知识库 |
| 4 | POST | `/api/documents` | 上传（multipart，**202**） | 知识库 |
| 5 | DELETE | `/api/documents/{document_id}` | 删除（**204**，无响应体） | 知识库 |
| 6 | GET | `/api/sessions` | 会话列表（最近活跃倒序） | 侧栏 |
| 7 | GET | `/api/sessions/{id}/messages` | 会话历史（正序，含 `role:"tool"`） | 对话台 |
| 8 | GET | `/api/eval/cases` | 评测集列表（**不含参考答案**） | 评测看板 |
| 9 | GET | `/api/eval/cases/{case_key}` | 单条详情（**含参考答案**） | 评测看板 |
| 10 | GET | `/api/eval/dataset` | 评测集指纹与分布 | 评测看板 |
| 11 | GET | `/api/eval/runs` | 运行列表（最新在前） | 评测看板 |
| 12 | GET | `/api/eval/runs/{run_id}` | 运行详情（含分类聚合） | 评测看板 |
| 13 | GET | `/api/eval/runs/{run_id}/cases` | 运行明细（可按 category/passed 过滤） | 评测看板 |
| 14 | GET | `/api/eval/runs/{run_id}/results/{case_key}` | 某题本次全部生成 | 评测看板 |
| 15 | GET | `/api/tools` | 工具清单 + server 状态 | 工具注册表 |

**除了 2、4、5，其余全是 GET 只读。**

### 6.2 TS 类型定义（`src/types/api.ts`）

**这一份可以直接用。** 字段名、可空性都是照 schema 逐字对过来的。

```ts
// ============ health ============
export interface HealthResponse {
  status: 'ok' | 'degraded';
  version: string;
  deps: { api: string; redis: string };
}

// ============ chat ============
export interface SourceItem {
  index: number;                 // 与回答里 [n] 对应
  source: string;                // 来源文档名/标题
  content: string;               // 命中的原文片段
  similarity: number | null;     // 余弦相似度，可能为 null
  page_ref: string | null;       // 如 "p.3"，仅 PDF，其他格式为 null
}
export interface ChatResponse {
  session_id: string;            // 标准 UUID（36 位带横杠）
  answer: string;
  sources: SourceItem[];
  invalid_citations: number[];   // 幻觉引用：回答里引用了但来源表没有的编号
  tool_calls: string[];          // ★ 只有工具名，没有耗时/入参（见 §5.2 G3）
}

// ============ documents ============
export interface DocumentOut {
  id: string;                    // UUID 字符串
  filename: string;
  file_type: string;             // pdf / docx / md / txt
  status: 'processing' | 'ready' | 'failed';
  chunk_count: number;           // ready 后才有意义
  error_message: string | null;  // status=failed 时才有值
  created_at: string;            // ISO 8601
}

// ============ sessions ============
export interface SessionItem {
  id: string;
  title: string;                 // 取自首条用户消息前 30 字
  updated_at: string;
}
export interface MessageItem {
  role: 'user' | 'assistant' | 'tool';
  content: string;               // 纯工具调用的 assistant 消息内容为空串
  tool_calls: ToolCallPayload[] | null;
  created_at: string;
}
/** messages 里的 tool_calls 是 LLM 原始的 function calling 结构，JSONB 原样返回 */
export interface ToolCallPayload {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };  // ★ arguments 是 JSON 字符串，要 parse
}

// ============ eval ============
export interface EvalCaseOut {
  id: number;
  case_key: string;              // A01 / B03 / C10
  category: 'doc_qa' | 'cross_doc' | 'tool_call';
  difficulty: 'easy' | 'medium' | 'hard';
  is_negative: boolean;          // 负例：语料里没有答案，正确行为是拒答
  question: string;
}
export interface EvalCaseDetailOut extends EvalCaseOut {
  reference: string;             // 参考答案（judge 的标尺）
  evidence: string;
  doc_slugs: string[];
  expected_tool: string | null;  // '__none__' 表示显式要求不调工具
}
export interface EvalDatasetOut {
  total: number;
  negative: number;
  by_category: Record<string, number>;
  by_difficulty: Record<string, number>;
  covered_slugs: string[];
  fingerprint: string;           // sha256 —— 变了说明题被改过
}
export interface EvalRunOut {
  id: number;
  config_name: string;                   // pure_vector / hybrid / hybrid_rerank
  dataset_fingerprint: string | null;
  corpus_fingerprint: string | null;
  judge_model: string | null;
  generation_runs: number | null;        // 每条题重复生成次数
  runs_per_case: number | null;          // 每份答案重复打分次数
  score_correctness: number | null;
  score_faithfulness: number | null;
  score_completeness: number | null;
  accuracy: number | null;               // ★ 分母是「题数」
  report_path: string | null;
  created_at: string;
}
export interface EvalCategoryStatOut {
  category: string;
  rows: number;                  // = 题数 × generation_runs
  cases: number;
  passed_rows: number;
  avg_correctness: number | null;
  avg_faithfulness: number | null;
  avg_completeness: number | null;
}
export interface EvalRunDetailOut extends EvalRunOut {
  by_category: EvalCategoryStatOut[];
  failure_breakdown: Record<string, number>;
  rows: number;                  // 明细总行数
  scored_rows: number;           // ★ 有分数的行数 —— 三维均分的真实分母（< rows）
  generation_inconsistent: number;  // 多次生成结论不一致的题数
}
export interface EvalCaseResultOut {
  id: number;
  case_key: string;
  category: string;
  generation_index: number;      // 第几次生成（0 起）
  answer: string;                // 被测系统的输出（不是答案页）
  tool_calls: string[] | null;
  sources_count: number;
  score_correctness: number | null;
  score_faithfulness: number | null;
  score_completeness: number | null;
  judge_runs: number;            // 实际成功打分次数
  passed: boolean;
  failure_reason: string | null;
  failure_reason_label: string;  // ★ 后端算好的中文标签，直接用
}
export interface EvalCaseResultDetailOut extends EvalCaseResultOut {
  retrieved_chunks: string;      // ⚠ 是一段文本，不是结构化片段列表（见 §5.7）
  judge_raw: unknown[] | null;
}

// ============ tools ============
export interface ToolItem {
  name: string;
  description: string;
  parameters: Record<string, unknown>;   // JSON Schema
  source: string;                        // 'local' | server 名
}
export interface MCPServerStatus {
  name: string;
  connected: boolean;
  tools: string[];
  tool_count?: number;           // ⚠ 可选：连接失败时这个字段不存在（见 §5.5）
  server_info?: { name: string | null; version: string | null } | null;
  protocol_version?: string;
  stderr_log: string | null;
  error?: string | null;
}
export interface ToolsResponse {
  total: number;
  by_source: Record<string, string[]>;
  servers: MCPServerStatus[];
  tools: ToolItem[];
}
```

### 6.3 ★ 数据源缺口总表（**实现前先看这个**）

| 页面 | 缺口 | 处理 |
|---|---|---|
| 对话台 | **无 SSE**（非流式） | §5.2.1 方案 A：一次渲染 + "思考中"态 |
| 对话台 | 消息 meta（耗时/ token / 成本）**无源** | 不渲染或留空 |
| 对话台 | 工具卡片的**耗时**无源 | 隐藏该格；入参/返回可走 messages 接口补 |
| 外壳 | **模型切换**无源 | 只读展示或去掉 |
| 外壳 | 用户信息无源（无用户系统） | 换环境标识或去掉 |
| 知识库 | **无"重试"接口** | failed 行只给"删除" |
| 评测看板 | **无"触发评测"接口** | 按钮禁用 + tooltip 说明用命令行 |
| 工具注册表 | 无耗时 / 无健康度 | 隐藏耗时；状态用 `servers[].connected` 近似 |
| 工具注册表 | **无"注册工具"接口** | 按钮禁用 + tooltip |
| **Trace 时间线** | **完全无接口** | 界面做完整 + mock |
| **检索检查器** | 只有一段字符串，**无结构化数据** | 界面做完整 + mock；可顺带原样展示字符串 |
| **分析页** | **无 `/api/metrics`**（D35 才做） | 界面做完整 + mock；或 M6 先不做 |

> **红线复述**：**不要为了让页面对上而虚构后端接口。** 缺的地方就让它缺着，用 mock 并标注。

### 6.4 mock 数字纪律

| 规则 | 说明 |
|---|---|
| **不要照抄 v0 稿的数字** | 它的评测数字（62.5% / 48 行 / 60 条 / 失败 30 条）**全部与实际不符**，实测对照见 §5.4.3 |
| **不要在组件里直接写 mock 常量** | 全部集中到 `src/mock/index.ts`，每个导出加注释说明"等哪个接口" |
| **要能一眼看出是 mock** | 建议：mock 数据驱动的页面，在 `PageHeader` 右上角加一个小的 `<Tag color="warning">示例数据</Tag>`。**这比默默显示假数字诚实得多，也更符合这个项目的性格** |
| **数字要自洽** | 若 mock 一个"50 题"的数据集，那么 `rows` / `scored_rows` / 各分类的题数必须能加起来对得上。**假数据算不平比没有数据更糟** |

### 6.5 mock 文件组织（`src/mock/index.ts`）

```ts
// src/mock/index.ts
// ⚠ 本文件只放"后端还没有对应接口"的数据。有接口的一律走 src/api/*。
// 每个导出的头部注释写明：等哪个接口、接口到位后删掉这里的哪个常量。

/** 等后端暴露 trace 查询接口（当前 Langfuse 数据无 REST 出口） */
export const MOCK_TRACES = [ /* { trace_id, name, total_ms, spans: [...] } */ ];

/** 等后端返回结构化检索片段（当前只有 retrieved_chunks 一段字符串） */
export const MOCK_RETRIEVAL = [ /* { rank_before, rank_after, doc, score_logit, is_gold } */ ];

/** 等 D35 的 GET /api/metrics（语义层） */
export const MOCK_METRICS = [ /* { metric, dimension, points: [{ date, value }] } */ ];
```

**mock 数据的形状建议直接对齐"将来后端应该返回什么"**——这样接口一到位，只改数据获取层。

---

## 7. 实现顺序与验收

### 7.1 实现顺序（5 天，**按此顺序做**）

> 这个顺序的原则：**先地基、后页面；先把"能真跑"的做完，把"只能做界面"的放后面**（这样万一时间不够，砍掉的是 mock 页，不影响可演示性）。

| 天 | 内容 | 关键产出 |
|---|---|---|
| **D29** | **① 工程脚手架**（`npm create vite` → 按 §2 加依赖与配置）<br>**② 主题骨架**（§4 的 5 个文件）<br>**③ API client**（`client.ts` + `/health` 打通）<br>**④ 外壳**（Layout + Sidebar + Topbar，7 个空页占位） | 浏览器能打开、侧栏能切页、主按钮是青色、主题能切、`/api/health` 返回 OK |
| **D30** | **对话台 ChatPage**（最重的一页）：消息流 + 发送 + 引用抽屉 + ToolCallCard | 能真发一轮对话并看到回答与引用 |
| **D31** | **知识库 KnowledgePage** + **评测看板 EvalPage** + 会话侧栏接真数据 | 上传/轮询/删除可用；评测三配置对比可看 |
| **D32** | **Trace 时间线**（自绘横向 span 图 + hover 浮层 + 失败态） | 界面完整（数据 mock） |
| **D33** | **工具注册表 ToolsPage** + **RAG 检索检查器** | 工具清单接真数据；检索检查器界面完整（数据 mock） |

> **⚠ 与 `DESIGN.md §10` 的顺序不同**：DESIGN 要求"Trace 页最先做，用它把调性钉死"。
> **偏离理由**：那是 v0 稿产生**之前**写的顺序（当时还没有已实现的视觉基线）。
> 现在 v0 稿已经把调性钉死了（色板逐字同源，见 §3.1），所以不再需要"用最难那页定调"。
> **另一个理由**：把两个 mock 页排在最后，正好是"超支时最先压"的位置。
> 若你认为应遵从 DESIGN 的顺序，请**先说明**再调整。

### 7.2 可量化验收（**逐条能当场数**）

| # | 验收项 | 判据 |
|---|---|---|
| 1 | dev server 起 | `npm run dev` 无报错，浏览器打开有界面 |
| 2 | **主题色生效** | AntD 主按钮 = 青 `#0E6F63`（**不是** AntD 默认 `#1677FF`） |
| 3 | **双主题同源** | 点切换 → `<html data-theme>` 变 **且** AntD 组件同时变（两者不能脱节） |
| 4 | 主题持久化 | 切到 dark → **刷新页面仍是 dark**（localStorage 生效） |
| 5 | dev proxy 打通 | 前端端口请求 `/api/health` 拿到后端 JSON |
| 6 | **无裸色值** | `grep -rnE "#[0-9a-fA-F]{3,6}" src/` 只应命中 `theme/palette.ts` 与 `tokens.css` |
| 7 | 表格行高 32px | DevTools 量 `<td>` 实际高度 ≈ **32px**（不是 44 / 54） |
| 8 | 字号基准 13px | DevTools 量正文 `font-size: 13px` |
| 9 | 圆角上限 6px | 全局搜 `border-radius`，**没有 ≥ 8px**，且**没有胶囊按钮** |
| 10 | 数字等宽 | 表格数字列右对齐 + `tabular-nums`（DevTools 看 computed style） |
| 11 | **评测看板首屏 ≥ 15 行** | 1440×900 下数一数（DESIGN §5.3） |
| 12 | **工具注册表首屏 ≥ 10 个** | 同上 |
| 13 | **Trace 页首屏 ≥ 12 个 span** | 同上 |
| 14 | 对话消息折叠后 ≤ 72px | 量一条带工具卡片的 Agent 消息 |
| 15 | 三态齐全 | 每个列表页都**实际看到过**空态 / 骨架屏 / 错误态（可断网或删 mock 触发） |
| 16 | 类型检查 | `npx tsc --noEmit` **零错误** |
| 17 | 无 emoji 图标 | 全局搜 emoji，**零命中**（R08） |
| 18 | 无渐变 | 全局搜 `gradient`，**只在进度条/光标处命中**（R07） |

### 7.3 交付自检清单

提交前逐条打勾：

- [ ] `npx tsc --noEmit` 零错误
- [ ] 上面 18 条验收**逐条实测过**（不是"应该没问题"）
- [ ] `src/mock/index.ts` 里每个导出都有"等哪个接口"的注释
- [ ] 所有"按钮点了没反应"的地方，要么禁用 + tooltip，要么已说明
- [ ] 没有把 `docs/frontend/DESIGN.md` 或 `V0-PROMPT.md` 的内容复制成新的重复文档
- [ ] 在浏览器里**把 7 个页面都点一遍**，并**切到 dark 再看一遍**（双主题是本次最容易漏的验收项）

---

## 8. 红线清单（**违反即返工**）

| # | 红线 | 为什么 |
|---|---|---|
| **R1** | **不换框架**：Vite + React + TS + AntD。不引 Next.js / Tailwind / shadcn | 项目的部署、目录、文档全部按此写好了 |
| **R2** | **不写裸色值**：任何 hex / 颜色名只能出现在 `theme/palette.ts` 与 `tokens.css` | 否则主题切换时它会"不跟着变"，且违反 DESIGN §9 |
| **R3** | **不改后端**，不动 `docs/` 下现有文档 | 你的工作在 `frontend/` 内闭环 |
| **R4** | **不编接口**：后端没有的接口，就让它缺着 + 走 mock 并标注 | 编出来的接口会让整个前端的对接层作废 |
| **R5** | **不引外部字体**，不引任何 CDN 资源 | 项目要能离线跑（DESIGN §4.1） |
| **R6** | **不引凭据**：`.env` 里的任何 key **绝不能**出现在前端代码里 | 项目红线 4：任何外部凭据不得进入代码 |
| **R7** | **不用 emoji 当图标**，不用渐变，不用 ≥ 8px 圆角 | DESIGN R03 / R07 / R08 |
| **R8** | **不为"好看"加阴影**：阴影只在浮层用 | DESIGN R02 |
| **R9** | **不照抄 mock 数字** | 见 §5.4.3 |
| **R10** | **不做 hero 结构**、不做入场动画、不做列表 stagger | DESIGN R10 + §3.7 动效预算 |

---

## 附录 A · 本文件与其它文档的关系

| 文档 | 关系 |
|---|---|
| `docs/PRD-v4.1-含前沿技术.md` | **功能范围权威**。本文档的页面清单、接口需求都从它来（§7 F9 / §9.7 / §8.3） |
| `docs/frontend/DESIGN.md` | **视觉唯一权威**。本文档 §3 / §4 大量引用它；冲突时以它为准 |
| `docs/frontend/V0-PROMPT.md` | 之前喂给 v0 的提示词包（含逐字核对过的数据契约附录 A）。**参考用，不是权威** |
| `~/Downloads/agent-forge-lite/` | v0 产出稿。**视觉基线**（结构 + CSS 可直接复用），但**技术栈不可复用** |
| **本文档 IMPL-SPEC.md** | **实现规格**。给实现者看的、可直接照着写代码的那一份 |

> **冲突裁决顺序**：功能听 PRD → 视觉听 DESIGN → 实现细节听本文档 → 具体形态参考 v0 稿。

## 附录 B · 已知的待办与不确定项

| 项 | 说明 | 状态 |
|---|---|---|
| SSE 流式 | 后端尚未实现；本文给的方案 A（一次渲染 + 思考中）是过渡态 | ⏳ 待后端 |
| Trace 页数据源 | Langfuse 有数据但无 REST 出口 | ⏳ 未排期 |
| 检索检查器的结构化数据 | 后端只返回一段字符串 | ⏳ 未排期 |
| `/api/metrics` | 语义层，D35 才做 | ⏳ 待 D35 |
| 模型切换 | 后端由 `.env` 决定，非运行时可选 | ⏳ 未排期 |
| 用户系统 | 项目无用户体系，`.user-row` 无意义 | ⏳ 未排期 |
| AntD `cssVar` 变量命名 | 未实跑验证，属推测 | ⏳ 待实跑 |
| 路由 | M6 用 `useState` 切页；是否需要真路由待定 | ⏳ 待定 |

---

*本文件由 `docs/frontend/DESIGN.md`（视觉权威）+ `docs/PRD-v4.1-含前沿技术.md`（功能权威）+ 后端实际代码（数据权威）三方核对生成。
所有接口字段、尺寸数值、颜色值均为实读，非估计。*
