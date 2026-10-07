# AgentForge-Lite · v0 前端生成提示词包

> **用途**：把这份文件里的文本**逐段复制**给 v0（https://v0.app），生成 AgentForge-Lite 的前端页面。
> **前置**：视觉规范来源是 `docs/frontend/DESIGN.md` (v1.1)；功能范围来源是 `docs/PRD-v4.1-含前沿技术.md` (§7 F9 / §9.7)。
> **版本** v1.0 · 2026-10-04

---

## 0. 先读这一段（**不要粘给 v0**）

### 0.1 一个必须知道的技术栈冲突

v0 的官方定位是"输出 shadcn/ui + Tailwind CSS 的 React 组件"，它的 FAQ 里**明确回答不支持 Ant Design**。而你的 PRD (§8.4) 定的是 **React + TS + Vite + Ant Design**，且已确认不改。

所以你有两种用法，**先选一条**：

| 路径 | 做法 | 代价 |
|---|---|---|
| **A · 强制 AntD**（本文件默认） | prompt 里明确指定 AntD v5，并贴上 §附录 B 的 `ThemeConfig` | v0 训练数据里 AntD 样本少，**代码质量不稳定**——可能混用 shadcn 组件、或漏装依赖。产出需要你人工修 |
| **B · 接受 shadcn** | 不提 AntD，让它按默认栈出 | 代码好看且完整，但**不能落进你的 AntD 项目**，只能当视觉稿看，落地要整个重写 |

**本文件按 A 写。** 理由是你要的是"能落地的前端项目"，技术栈错了产出就是废的。但你要预期：**v0 会犯错，你是 10 年前端，修它是你的活。**

### 0.2 v0 的运行环境与你要的不一样

v0 的预览跑在 **Next.js App Router** 上，你的项目是 **Vite + React**。这不影响组件代码的可移植性，但要注意：

- **要求它写纯客户端组件**（文件顶部加 `'use client'`，或明确说"不使用 Next.js 专属 API"）
- **不要让它写路由**（`app/page.tsx` 那套）—— 你要的是 `src/pages/XxxPage.tsx`，路由由你的 Vite 项目决定
- 数据请求**先全用 mock 常量**，不要接真实 API（接后端是生成完之后你自己的活）

### 0.3 分次生成，一次一页

**不要一次要 5 个页面。** v0 的上下文有限，一次要多了每页都会糊。正确节奏：

```
第 1 次：全局块 + 骨架（侧边栏 + 顶栏 + 一个空内容区）
        ↓ 满意后
第 2 次：全局块（精简版）+ 对话台
        ↓ 满意后
第 3 次：……逐页
```

每次**只改一页**，改到满意再开下一页。中途要调整视觉，直接说"把 X 改成 Y"，不要重开对话。

### 0.4 三段式用法

本文件后面分三块，**用法不同**：

| 块 | 位置 | 怎么用 |
|---|---|---|
| **全局块** | §1 | 每次新开一个生成对话，**先粘它**。它是"人设 + 硬约束" |
| **页面块** | §2 P1–P7 | 一次粘一段，生成一页 |
| **附录** | §3 / §4 | 附录 A 是 mock 数据（**必须贴**，见下）；附录 B 是 AntD 主题代码 |

### 0.5 ★ 为什么 mock 数据必须给足

"AI 感"最显眼的特征之一是**每屏只有 3 行假数据 + 大片留白**。你给了真实的字段形状和足够行数（20–30 行），它就没法退化成"三行 demo"。**这一条比配色更能决定成品像不像真工具。**

---

## 1. 全局块（每次生成前先粘这段）

> 复制下面整段。

```text
你在为一个叫 AgentForge-Lite 的项目做前端界面。这是一个"Agent 运行时平台"的工程控制台，用户是工程师——他们要在这里观察 Agent 到底干了什么。请严格按下面的约束生成，不要加入你自己的审美偏好。

【产品定位】
这是一台机器的仪表盘，不是一张宣传单。用户不是来被说服购买的，是来查状态的。所以：
- 优先信息密度，不是呼吸感
- 优先可比对的结构，不是视觉惊喜
- 优先真实状态（跑着/卡住/失败/空了），不是永远好看的演示态
- 这是一个开发者工具，审美基准是 Linear / Vercel Dashboard / Langfuse / Datadog 那一类，不是现代 SaaS 落地页

【技术栈 · 硬约束，不可替换】
- React 18 + TypeScript，纯客户端组件（可用 'use client'，但不要用 Next.js 专属 API）
- 组件库必须是 Ant Design v5，用 @ant-design/icons 提供图标
- 不要使用 Tailwind CSS。自定义样式用 CSS Modules
- 图表用 echarts + echarts-for-react
- 所有数据先用文件内的 mock 常量，不要写 fetch、不要建 API 层

【色彩 · 必须使用下面这些精确色值，不要自己挑颜色】
浅色主题：
  页面底 canvas        #F5F6F8
  卡片面 surface       #FFFFFF
  浮层面 surface-raised #FFFFFF
  凹入区 sunken        #ECEEF1  (代码块、时间线轨道、输入框底)
  分隔线 border-subtle #E4E7EB
  描边 border-default  #D5D9DF
  正文 text-primary    #14181E
  次要文字 text-secondary #4A5361
  元信息 text-tertiary #667080
  强调色 accent        #0E6F63  (青)
  成功 success         #0B7A46
  危险 danger          #C0271D
  警告 warning         #8A5A08
  信息 info            #0B6BA8

深色主题（同时实现，用 <html data-theme="dark"> 切换）：
  canvas #131519 / surface #191C21 / surface-raised #1F2329 / sunken #0E1013
  border-subtle #2A2F36 / border-default #33383F
  text-primary #F2F3F5 / text-secondary #A8B0BC / text-tertiary #7B8494
  accent #4FD1B8 / success #3DD68C / danger #FF7A6E / warning #E0A83A / info #5FB4E8

颜色使用纪律（重要）：
- 强调色(青)只用于交互：当前选中项、链接、焦点环、主按钮。永远不用它表达状态
- 成功(绿)只用于状态，永远不用它表达交互。因为青和绿是邻近色相，同一条信息里不得同时出现
- 状态一律用「8px 色点 + 文字」表达，禁止铺成整块彩色背景
- 强调色的像素面积不超过全屏 3%

【字体与字号】
- 正文字体栈：-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif
- 等宽字体栈：'SF Mono', 'JetBrains Mono', Menlo, Consolas, monospace
- 基准字号 13px（不是 16px！），行高 20px
- 字号表：页面标题 20/28、区块标题 16/24、卡片标题 14/20、正文 13/20、元信息 12/16、代码与数字 12/16 等宽
- 字重只有 400 和 500 两档，不用 600/700
- 所有会被纵向比较的数字（耗时、token 数、得分、ID、指纹）必须用等宽字体，并加 CSS 属性 font-variant-numeric: tabular-nums，表格里右对齐

【密度 · 可量化的要求】
- 4px 基准网格，所有间距是 4 的倍数
- 控件高度 32px（小号 28 / 主操作 36）
- 列表与表格行高 32px，不要 48px 那种"透气"版式
- 表格单元格内边距：上下 6px，左右 12px
- 页面内边距 16px；区块间距 16/24/32 三档
- 卡片内边距 12px（紧凑）或 16px（默认）

【十条硬规则 · 违反任何一条即为不合格】
R01 先密度后留白。一屏（1440×900）至少 20 行有效信息，列表行高上限 36px
R02 层级用「背景微差 + 1px 描边」表达，不用阴影。卡片一律不加阴影，加描边。阴影只允许出现在浮层（下拉/弹窗/提示）
R03 圆角只有两档：容器 6px、控件 4px。禁止 8px 及以上的圆角，禁止胶囊按钮
R04 强调色像素面积不超过全屏 3%
R05 所有数字用等宽字体 + tabular-nums，表格里右对齐
R06 状态用 8px 色点 + 文字，禁止用整块彩色背景
R07 禁止任何渐变。唯一例外：流式输出光标、运行中的进度条
R08 禁止用 emoji 当图标。统一用 @ant-design/icons，同层级图标尺寸一致
R09 空态/加载/错误三态必须真实设计。禁止出现 "Loading..." 字样，禁止留白页
R10 禁止 hero 结构（居中大标题 + 副标题 + 两个按钮）。这里不是落地页

【明确不要的东西】
- 不要大圆角、大留白、渐变 hero、三张 Feature 卡片并排
- 不要 Ant Design 的出厂默认长相：不要 #001529 深蓝黑顶栏和侧边栏，不要默认蓝 #1677FF，顶栏高度 48px 不是 64px
- 不要 Dify 那种玻璃拟态、发光描边、装饰性渐变
- 不要 16px 基准字号、36px 起的大标题
- 不要 Dribbble 概念稿那种炫技动效

【三态要求】
- 加载：骨架屏，形状必须与真实内容一致（表格骨架就画 5 行格子，不是居中转圈）
- 空：一句人话 + 一个具体动作按钮。例：「还没有评测记录。跑一次评测 →」
- 错误：必须给可操作的下一步，不能只贴报错原文。例：「重排模型未找到。检查 models/bge-reranker-base 是否存在 →」
- 长时运行（超过 2 秒）：必须有进度或耗时显示，不能只有一个转圈

【动效预算】
只允许 150–200ms 的 opacity / transform 过渡。禁止入场动画，禁止列表逐项延迟出现（stagger），禁止任何循环播放的装饰动画（唯一例外：运行中的进度条）。

【输出要求】
- 每个页面一个独立文件，放在 src/pages/ 下
- 复用组件放在 src/components/ 下
- 主题配置放在 src/theme/ 下
- 用示例数据把界面填满（我会在下一段给出数据形状和足够的行数），不要只放两三行
- 代码写完后，用一段话说明：你用了哪些 AntD 组件、哪些地方是你自己判断的、有哪些地方你不确定
```

---

## 2. 页面块（一次一页）

> **生成顺序**：P1 → P2 → P3 → P4 → P5 → P6 →（P7 可选）
>
> 这个顺序和 PRD 的排期（D29–D36）一致。**P7 放最后**，因为它是唯一一页 AntD 没有现成组件的、最依赖自绘的页面，v0 出错率最高，适合等你已经和它磨合过几轮之后再提。

---

### P1 · 应用外壳（侧边栏 + 顶栏 + 主题切换）

```text
先只做应用外壳，不要做任何具体页面内容。

整体布局：
- 左侧固定 Sidebar，宽 200px，可折叠到 48px
- 顶部 Header，高 48px
- 右侧主内容区，背景用 canvas (#F5F6F8)，内边距 16px
- Sidebar 和 Header 的背景用 surface (#FFFFFF)，与 canvas 靠 1px 描边（border-subtle #E4E7EB）分隔，不要用阴影

Sidebar 内容（从上到下）：
1. 顶部品牌区：一行 "AgentForge" 文字（15px，字重 500）+ 右侧一个折叠按钮（图标用 MenuFoldOutlined / MenuUnfoldOutlined）
2. 导航菜单（Ant Design Menu，mode="inline"），五项：
   - 对话台    MessageOutlined      路由 /chat
   - 知识库    DatabaseOutlined     路由 /knowledge
   - 评测看板  ExperimentOutlined   路由 /eval
   - 分析      LineChartOutlined    路由 /analytics
   - 工具注册表 ApiOutlined         路由 /tools
   当前选中项用强调色(#0E6F63)表示，不要用整块彩色背景
3. 底部：会话历史区
   - 小标题"最近会话"（12px，text-tertiary）
   - 列出 6–8 条会话，每条两行：标题（13px，单行省略号截断）、时间（11px，text-tertiary）
   - 当前选中的会话用强调色左边框(2px) + surface 背景表示

Header 内容：
- 左侧：当前页面标题（15px，字重 500），不要面包屑
- 右侧：三个东西，从右到左
  1. 主题切换按钮（SunOutlined / MoonOutlined），点击切换 html 的 data-theme 属性在 light/dark 之间，并存进 localStorage
  2. 一个状态指示器：8px 色点（success 绿 #0B7A46）+ 文字 "API 正常"（12px）
  3. 模型选择器（Ant Design Select，宽 140px，size="small"），选项为 deepseek-chat / gpt-4o-mini

其他要求：
- 主内容区放一个占位：一句文字"选择左侧导航开始"，居中但字号 13px（不要做成大标题）
- 整体必须同时适配 light/dark 两个主题
- 实现主题切换的完整逻辑：html 上的 data-theme 属性、localStorage 持久化、Ant Design 的 ConfigProvider theme 跟着同步
- 主题配置文件按这个结构建：src/theme/palette.ts（色值）、src/theme/antdTheme.ts（Ant Design 的 ThemeConfig，导出 lightTheme / darkTheme 两个对象）

Ant Design 的 ThemeConfig 里至少要覆盖这些 token：
  colorPrimary: 青强调色、colorSuccess / colorWarning / colorError / colorInfo 按上面给的语义色
  colorBgLayout: canvas 色、colorBgContainer: surface 色、colorBgElevated: surface-raised 色
  colorBorder: #D5D9DF、colorBorderSecondary: #E4E7EB
  colorText / colorTextSecondary / colorTextTertiary 按文字层三档
  fontSize: 13、fontSizeSM: 11、fontSizeLG: 15
  controlHeight: 32、borderRadius: 6、borderRadiusSM: 4、borderRadiusLG: 6
  lineHeight: 1.54、fontWeightStrong: 500
  fontFamily 和 fontFamilyCode 按上面给的字体栈
  components.Layout: headerBg 和 siderBg 都要覆盖成 surface 色（Ant Design 默认是 #001529，必须改）
  components.Layout.headerHeight: 48、headerPadding: '0 16px'
```

---

### P2 · 对话台 ChatPage

> 这是 PRD 的 F9.1，也是演示时用得最多的一页。

```text
现在做对话台页面。这是整个产品最核心的一页。

页面结构（上到下）：
1. 顶部一行工具栏（高 40px，底部 1px 描边）
   - 左：会话标题（14px，字重 500）+ 会话 ID（12px 等宽，text-tertiary，显示前 8 位加省略号）
   - 右：两个按钮——「新建会话」（图标 PlusOutlined）、「导出」（图标 DownloadOutlined），都用 size="small"
2. 中间消息流区域（可滚动，占据剩余高度）
3. 底部输入区（固定，背景 surface，顶部 1px 描边）
   - Ant Design Input.TextArea，3 行高，placeholder 写「问点什么…（Enter 发送，Shift+Enter 换行）」
   - 右下角一个发送按钮，主色，显示文字"发送"

消息流的设计（这是重点，请严格按这个来）：

用户消息：
- 左对齐（不要右对齐、不要气泡尾巴）
- 上方一行：角色标签"你"（12px，text-secondary）
- 正文 13px，行高 20px
- 下方一行元信息：时间（12px 等宽，text-tertiary）
- 背景用 sunken (#ECEEF1)，左侧 2px 竖线用 border-strong 色，圆角 4px，内边距 12px

Agent 消息：
- 左对齐
- 上方一行：角色标签"Agent"（12px，text-secondary）+ 模型名（12px 等宽，text-tertiary）
- 正文 13px
- **正文里的引用编号 [1] [2] 要处理成可点的样式**：文字颜色用强调色、带浅色下划线、鼠标变成 pointer。点击后在页面右侧滑出一个抽屉（Ant Design Drawer），显示这条引用的原文
- 下方一行元信息（内联，12px 等宽，text-tertiary，用 · 分隔）：耗时 1240ms · 1,238 tokens · ¥0.003
- 背景不做底色，与页面同底，靠上下的 1px 分隔线（border-subtle）区分

工具调用卡片（在 Agent 消息里，正文之前）：
- 用可折叠结构，折叠态一行显示：状态色点 + 工具名（12px 等宽）+ 来源标签 + 耗时（右对齐）
- 例如：● harness__list_pipelines  来源:Harness   820ms
- 来源标签用 Ant Design Tag，size="small"，背景透明、1px 描边，文字 11px
- 展开后显示三块：
  · 入参：JSON 格式，用等宽字体，背景 sunken，圆角 4px
  · 返回摘要：等宽字体，最多 6 行，超出滚动
  · 元信息一行：span id、耗时、状态
- 折叠态整行高度不超过 28px（这一条是硬要求——一屏要能放很多条）

引用抽屉（Citation Drawer）：
- 从右侧滑出，宽 420px
- 顶部：「引用 [1]」标题 + 关闭按钮
- 内容分三块：
  1. 来源文档名（14px，字重 500）+ 页码标签（如 p.3）
  2. 相似度分数（等宽数字，右对齐，标注"余弦相似度"）
  3. 原文片段（13px，行高 22px，背景 sunken，内边距 12px）
- 底部：「在知识库中打开」按钮

流式输出的视觉：
- 正在生成时，正文末尾显示一个 2px 宽、14px 高的竖条光标，用强调色，做一个 1 秒的闪烁（这是唯一允许的循环动画）
- 生成中的消息，元信息区显示「生成中…」

页面底部状态栏（可选，一行，高 28px，12px 文字，text-tertiary）：
本次会话 3 轮 · 2 次工具调用 · 累计 4,102 tokens

必须实现的三个状态（都请真的画出来，不要只画数据齐全的那一屏）：
- 空态：还没有消息时，居中显示「还没有对话。在下面输入你的第一个问题 →」，带一个示例问题列表（3 条，可点击，纯文字按钮样式）
- 加载态：消息流区域显示骨架屏，用 Ant Design Skeleton 画 3 组（每组两行，宽度 100% / 60%）
- 错误态：如果 Agent 返回失败，在消息流里插一条错误卡片：红色色点 + 「调用失败：LLM 服务超时」+ 一个「重试」按钮

示例数据：请用下面 3 轮对话填满，其中第 2 轮带工具调用、第 3 轮带引用。

【数据放在文件顶部作为常量】

const mockMessages = [
  {
    role: 'user',
    content: '这个项目为什么不用 LlamaIndex？',
    created_at: '2026-10-04 10:12:03'
  },
  {
    role: 'assistant',
    content: '主要是因为控制权。[1] 项目把检索链路拆成了 BM25 与向量两路，再用 RRF 融合，最后过一层重排。LlamaIndex 把这些步骤封装在它自己的抽象里，做消融实验时不容易只改其中一环。[2]',
    model: 'deepseek-chat',
    latency_ms: 1240,
    tokens: 1238,
    cost: 0.003,
    tool_calls: [
      {
        name: 'search_documents',
        source: 'local',
        latency_ms: 86,
        status: 'ok',
        arguments: { query: '为什么不用 LlamaIndex', top_k: 5 },
        result_summary: '命中 5 个片段，最高相似度 0.83'
      },
      {
        name: 'harness__list_pipelines',
        source: 'harness',
        latency_ms: 820,
        status: 'ok',
        arguments: { limit: 10 },
        result_summary: '返回 7 条流水线，其中 2 条失败'
      }
    ],
    sources: [
      { index: 1, source: '技术选型说明.md', content: '本项目检索层不引入 LlamaIndex……', similarity: 0.83, page_ref: null },
      { index: 2, source: '消融实验记录.md', content: '为了能只替换单一环节……', similarity: 0.79, page_ref: 'p.3' }
    ],
    invalid_citations: []
  },
  {
    role: 'user',
    content: '那评测集现在多少条？',
    created_at: '2026-10-04 10:13:40'
  },
  {
    role: 'assistant',
    content: '当前 60 条，分四类：文档问答 30 条、跨文档推理 10 条、工具调用 10 条、多步分析 10 条。',
    model: 'deepseek-chat',
    latency_ms: 980,
    tokens: 640,
    cost: 0.0016,
    tool_calls: [],
    sources: [],
    invalid_citations: []
  }
]

const sessions = [
  { id: 'a3f2c1d8-...', title: '这个项目为什么不用 LlamaIndex？', updated_at: '10:13' },
  { id: 'b7e4a2f9-...', title: '最近三次评测的准确率趋势', updated_at: '昨天' },
  { id: 'c1d9b3e7-...', title: 'MCP 的工具怎么注册进来的', updated_at: '昨天' },
  { id: 'd5a8c2f4-...', title: '切片大小改成多少合适', updated_at: '10-02' },
  { id: 'e9b1d6a3-...', title: '重排模型用的什么', updated_at: '10-02' },
  { id: 'f3c7e9b2-...', title: 'Harness 的 11 个工具都是什么', updated_at: '10-01' }
]
```

---

### P3 · 知识库 KnowledgePage

> PRD 的 F9.2。这一页有个特殊要求：**要能看见处理中的状态**。

```text
现在做知识库页面。核心是"上传 → 处理 → 可检索"这条状态流要被看见。

页面结构：
1. 顶部工具栏
   - 左：标题"知识库" + 右侧一个数字徽标显示文档总数
   - 右：「上传文档」主按钮（图标 UploadOutlined）
2. 中部：文档列表（主要区域）
3. 底部：一行统计（12px，text-tertiary）：共 8 份文档 · 已就绪 7 · 处理中 1 · 共 412 个切片

文档列表（用 Ant Design Table）：
列定义：
- 文件名：13px，左侧带文件类型图标（PDF / Word / Markdown / 文本各不同图标），文件名后可跟一个页码数标签
- 类型：12px，text-secondary
- 状态：8px 色点 + 文字
  · 已就绪 → success 绿 #0B7A46
  · 处理中 → 用一个旋转的小图标（LoadingOutlined），这是唯一允许的持续旋转动画
  · 失败 → danger 红 #C0271D
- 切片数：等宽数字，右对齐（处理中时显示 "—"）
- 上传时间：12px 等宽，格式 MM-DD HH:mm
- 操作：一个"删除"文字按钮（用 danger 色），点击弹 Modal 确认

处理中的行要特殊处理：
- 该行显示一条 2px 高的进度条（在文件名下方），用强调色，做不确定进度动画（左右来回移动那种）
- 状态文字显示"处理中 · 已用 3.4s"

失败的行：
- 状态点用 danger 色
- 额外一行小字说明原因（12px，danger 色），例："PDF 解析失败：文件已损坏"
- 操作列额外给一个"重试"按钮

空态：
- 整页居中显示：「知识库是空的。上传第一份文档 →」
- 下面给一行小字提示（12px，text-tertiary）：支持 PDF / DOCX / MD / TXT，单个文件不超过 20MB

上传交互（用 Ant Design Upload.Dragger，放在一个 Modal 里）：
- 虚线描边区域，文字："点击或拖拽文件到此处"
- 下面一行小字：支持 PDF / DOCX / MD / TXT
- 上传后 Modal 不关闭，改为显示"已受理，正在处理…"的状态提示

删除确认 Modal：
- 标题："删除《文件名》？"
- 内容：一句风险说明——"该文档及其 42 个切片将被删除。已产生的对话引用不会失效。"
- 这两个按钮：取消 / 删除（危险色）

示例数据（8 行，请全部渲染出来）：

const mockDocuments = [
  { id: 'd1', filename: 'AgentForge 技术选型说明.md', file_type: 'md',   status: 'ready',      chunk_count: 48,  created_at: '2026-10-03 14:22' },
  { id: 'd2', filename: 'MCP 协议要点.pdf',            file_type: 'pdf',  status: 'ready',      chunk_count: 96,  created_at: '2026-10-03 14:25' },
  { id: 'd3', filename: '评测方法论.md',               file_type: 'md',   status: 'ready',      chunk_count: 72,  created_at: '2026-10-02 09:10' },
  { id: 'd4', filename: '消融实验记录-2026-09-30.md',  file_type: 'md',   status: 'ready',      chunk_count: 34,  created_at: '2026-10-02 09:12' },
  { id: 'd5', filename: 'Harness 接入说明.docx',       file_type: 'docx', status: 'ready',      chunk_count: 61,  created_at: '2026-10-01 16:40' },
  { id: 'd6', filename: 'RAG 检索链路设计.md',         file_type: 'md',   status: 'ready',      chunk_count: 55,  created_at: '2026-10-01 16:45' },
  { id: 'd7', filename: '老版架构图.pdf',              file_type: 'pdf',  status: 'failed',     chunk_count: 0,   created_at: '2026-10-04 10:05', error_message: 'PDF 解析失败：文件已损坏或加密' },
  { id: 'd8', filename: '面试问答整理.md',             file_type: 'md',   status: 'processing', chunk_count: 0,   created_at: '2026-10-04 10:14', elapsed_sec: 3.4 }
]
```

---

### P4 · 评测看板 EvalPage

> PRD 的 F9.3。这一页的核心是**口径必须写在界面上**——「分母是几」这件事在文档里说清了不算，得让看界面的人自己看明白。

```text
现在做评测看板页面。这一页非常特殊：它显示的是统计结果，而统计的"分母"有多个口径，界面上必须把口径写清楚，否则数字会被误读。

页面结构：
1. 顶部工具栏
   - 左：标题"评测看板"
   - 右：「运行新评测」按钮（主色，图标 PlayCircleOutlined）+ 一个配置选择器（Ant Design Select，选项：pure_vector / hybrid / hybrid_rerank）
2. 运行列表区（横向可切换，选中的运行决定下面显示什么）
   - 每个运行卡片显示：配置名、准确率、时间
   - 选中的卡片用强调色描边（1px）+ 微弱的背景差，不要用整块彩色
3. 汇总指标区（4 个指标并排）
4. 分类聚合表格（按题目类别）
5. 失败模式分布
6. 逐题明细表格

【第 1 个硬要求：可比性四件套必须显示】
每个运行卡片上，要显示这四个元信息（因为它们缺一个，分数就不能跟别的运行比）：
  配置名 · 题集指纹 · 语料指纹 · judge 模型 / 重复次数
指纹用等宽字体，只显示前 8 位加省略号，例如 32d4d63c…，鼠标悬停用 Tooltip 显示完整的。字号 11px，text-tertiary。

【第 2 个硬要求：口径写在表头】
汇总指标区用 4 个数字卡片（不要用 Ant Design 的 Statistic 组件，那个默认样式太"中后台"，请自己用 div 画）：
  准确率        62.5%      下方小字：按题计算，每题多次生成先取多数结论
  正确性均分     4.21       下方小字：按行计算，分母是 48 行（有分数的行）
  引用忠实度     3.88       下方小字：分母同上
  完整性        4.05       下方小字：分母同上
数字用 24px 等宽字体。**下面那行口径说明是必须的，不是可选装饰。**

分类聚合表格（Ant Design Table，size="small"）：
列：类别 | 题数 | 明细行数 | 通过行数 | 正确性 | 忠实度 | 完整性
类别列显示中文名，并在括号里标注英文键：
  doc_qa    → 文档问答
  cross_doc → 跨文档推理
  tool_call → 工具调用
数值列全部右对齐 + 等宽字体。分数低于 4.0 的用 warning 色文字。

【第 3 个硬要求：工具题要标注不计分】
分类表下方加一行注解（12px，text-tertiary）：
"工具调用类题目只判定调用序列是否正确，不送 judge 判内容，因此三维度得分为空（显示为 —）。引用均分时请使用"明细行数"作为分母，不是题目总数。"

失败模式分布：
横向条形（不要用饼图）。每行：失败原因中文标签 + 条数 + 一条按比例宽度的横条。
横条颜色：用 danger 色的不同透明度，不要彩虹色。
示例：检索未命中 12 / 引用越界 5 / 答案不完整 8 / 工具选错 3 / 其他 2

逐题明细表格：
列：题号 | 类别 | 第几次生成 | 答案摘要 | 引用数 | 三维分数 | 结果 | 失败原因
- 题号用等宽字体（如 A03、C07）
- 答案摘要限一行，超出省略
- 三维分数显示成 "4 / 5 / 4" 这种紧凑格式，等宽字体
- 结果列：一个 8px 色点 + "通过"/"未通过"
- 支持按类别和通过与否过滤（表格上方的两个 Select）
- 点击某一行，从右侧滑出抽屉，显示该题的完整答案、检索片段、判定依据

抽屉内容（宽 520px）：
- 题面原文
- 各行生成结果并排对比（这是关键：同一题的多次生成要能并排看，才能看出"这道题飘不飘"）
- 每行显示：答案全文、三个分数、judge 的原始输出

示例数据：

const mockRuns = [
  { id: 6, config_name: 'hybrid_rerank', accuracy: 0.625, dataset_fingerprint: '32d4d63c9e1a...', corpus_fingerprint: 'a7797b06fd3c...', judge_model: 'deepseek-chat', generation_runs: 1, runs_per_case: 3, score_correctness: 4.21, score_faithfulness: 3.88, score_completeness: 4.05, created_at: '2026-09-30 18:22' },
  { id: 5, config_name: 'hybrid',        accuracy: 0.583, dataset_fingerprint: '32d4d63c9e1a...', corpus_fingerprint: 'a7797b06fd3c...', judge_model: 'deepseek-chat', generation_runs: 1, runs_per_case: 3, score_correctness: 4.02, score_faithfulness: 3.61, score_completeness: 3.94, created_at: '2026-09-30 17:05' },
  { id: 3, config_name: 'pure_vector',   accuracy: 0.500, dataset_fingerprint: '32d4d63c9e1a...', corpus_fingerprint: 'a7797b06fd3c...', judge_model: 'deepseek-chat', generation_runs: 1, runs_per_case: 3, score_correctness: 3.78, score_faithfulness: 3.40, score_completeness: 3.85, created_at: '2026-09-30 15:40' }
]

const mockByCategory = [
  { category: 'doc_qa',    cases: 30, rows: 30, passed_rows: 21, avg_correctness: 4.40, avg_faithfulness: 4.12, avg_completeness: 4.25 },
  { category: 'cross_doc', cases: 10, rows: 10, passed_rows: 5,  avg_correctness: 3.80, avg_faithfulness: 3.40, avg_completeness: 3.70 },
  { category: 'tool_call', cases: 10, rows: 10, passed_rows: 7,  avg_correctness: null, avg_faithfulness: null, avg_completeness: null }
]

const mockFailures = [
  { reason: '检索未命中', count: 12 },
  { reason: '答案不完整', count: 8 },
  { reason: '引用越界',   count: 5 },
  { reason: '工具选错',   count: 3 },
  { reason: '其他',       count: 2 }
]

const mockCaseResults = [
  { case_key: 'A03', category: 'doc_qa',    generation_index: 0, answer: 'BM25 打分是应用层自己算的……', sources_count: 5, s1: 5, s2: 4, s3: 5, passed: true,  failure_reason: null },
  { case_key: 'A07', category: 'doc_qa',    generation_index: 0, answer: '根据文档，切片大小是 512……', sources_count: 4, s1: 4, s2: 4, s3: 4, passed: true,  failure_reason: null },
  { case_key: 'A12', category: 'doc_qa',    generation_index: 0, answer: '这个文档里没有提到……',       sources_count: 2, s1: 5, s2: 5, s3: 5, passed: true,  failure_reason: null },
  { case_key: 'A18', category: 'doc_qa',    generation_index: 0, answer: 'RRF 的 k 值取 60……',          sources_count: 5, s1: 4, s2: 5, s3: 4, passed: true,  failure_reason: null },
  { case_key: 'A22', category: 'doc_qa',    generation_index: 0, answer: '重排模型使用 bge……',          sources_count: 3, s1: 3, s2: 2, s3: 3, passed: false, failure_reason: 'low_score' },
  { case_key: 'A25', category: 'doc_qa',    generation_index: 0, answer: '不确定，文档没有说明……',     sources_count: 0, s1: 1, s2: 5, s3: 2, passed: false, failure_reason: 'retrieval_miss' },
  { case_key: 'A28', category: 'doc_qa',    generation_index: 0, answer: '切片重叠比例是 10%……',        sources_count: 4, s1: 5, s2: 4, s3: 5, passed: true,  failure_reason: null },
  { case_key: 'A30', category: 'doc_qa',    generation_index: 0, answer: '上下文窗口是 8 轮……',         sources_count: 5, s1: 4, s2: 4, s3: 4, passed: true,  failure_reason: null },
  { case_key: 'B02', category: 'cross_doc', generation_index: 0, answer: '综合两份文档来看……',         sources_count: 5, s1: 4, s2: 3, s3: 4, passed: true,  failure_reason: null },
  { case_key: 'B05', category: 'cross_doc', generation_index: 0, answer: '两份文档的口径不一致……',     sources_count: 4, s1: 3, s2: 2, s3: 3, passed: false, failure_reason: 'incomplete' },
  { case_key: 'B08', category: 'cross_doc', generation_index: 0, answer: '第一份说了 A，第二份说了 B……', sources_count: 3, s1: 4, s2: 4, s3: 4, passed: true, failure_reason: null },
  { case_key: 'B10', category: 'cross_doc', generation_index: 0, answer: '无法从给定文档得出……',       sources_count: 1, s1: 2, s2: 4, s3: 2, passed: false, failure_reason: 'retrieval_miss' },
  { case_key: 'C01', category: 'tool_call', generation_index: 0, answer: '现在时间是……',               sources_count: 0, s1: null, s2: null, s3: null, passed: true,  failure_reason: null, tool_calls: ['current_time'] },
  { case_key: 'C04', category: 'tool_call', generation_index: 0, answer: '库存少于 10 的商品有……',     sources_count: 0, s1: null, s2: null, s3: null, passed: true,  failure_reason: null, tool_calls: ['inventory__query_inventory'] },
  { case_key: 'C07', category: 'tool_call', generation_index: 0, answer: '我直接回答，不需要查询……',   sources_count: 0, s1: null, s2: null, s3: null, passed: false, failure_reason: 'tool_miss', tool_calls: [] },
  { case_key: 'C09', category: 'tool_call', generation_index: 0, answer: '最近失败的流水线有……',       sources_count: 0, s1: null, s2: null, s3: null, passed: true,  failure_reason: null, tool_calls: ['harness__list_pipelines'] },
  { case_key: 'C10', category: 'tool_call', generation_index: 0, answer: '这个问题不需要工具……',       sources_count: 0, s1: null, s2: null, s3: null, passed: false, failure_reason: 'tool_miss', tool_calls: ['search_documents'] }
]
```

---

### P5 · 分析页 AnalyticsPage

> PRD 的 F9.7。这一页是"自然语言提问 → Agent 多步分析 → 出图"。

```text
现在做分析页面（AnalyticsPage）。用户在这里用自然语言提问，Agent 会调用指标查询工具，最后出图并给结论。

页面采用左右分栏布局：
- 左侧：对话区，宽 380px，固定
- 右侧：图表与结论区，占据剩余宽度

左侧对话区：
- 顶部标题"分析"（15px，字重 500）
- 下面是一系列"分析轮次"，每一轮包含：
  1. 用户提问（13px，背景 sunken，圆角 4px，内边距 12px）
  2. Agent 的思考步骤列表 —— 这是这一页最需要设计的地方：
     每一步一行，显示：步骤序号（等宽，12px）+ 图标（调用工具用 ApiOutlined / 结论用 BulbOutlined）+ 说明文字（12px，text-secondary）
     例如：
       ① 查询 eval_accuracy 指标（最近 3 次运行）      124ms
       ② 查询 eval_failures_by_category 下钻           98ms
       ③ 查询 chunk_quality 定位根因                  76ms
     每一步右侧显示耗时（等宽，12px，text-tertiary）
     步骤之间用 1px 竖线连接，表示序列
  3. 底部的输入框（同对话台）

右侧图表区：
- 顶部：标题 + 数据来源标签
- 中部：ECharts 图表
- 底部：Agent 的结论文字

图表要求（用 echarts-for-react）：
- 主题色必须跟随浅色/深色主题切换（ECharts 不吃 CSS 变量，必须把两套色值显式传进 option，切换主题时重建图表实例）
- 折线图：线宽 1.5px，不要渐变填充，不要平滑曲线（用直线段）
- 柱子：圆角 2px（不要大的圆角），宽度控制在 60%
- 网格线：只保留水平方向的，颜色用 border-subtle 色
- 坐标轴文字：12px，颜色用 text-tertiary 色
- 不要图例遮罩、不要动画（animation: false）
- 数据点标记用 4px 的圆点

需要画两张图（上下排列，各占约 220px 高）：

图 1：准确率趋势折线图
标题：评测准确率趋势
数据来源标签：【SQL】
X 轴：run 6 / run 5 / run 3
Y 轴：0 到 1，标签显示成百分比
两条线：hybrid_rerank 用强调色、hybrid 用 info 蓝

图 2：分类得分柱状图
标题：各类型题目得分
数据来源标签：【SQL】
X 轴：文档问答 / 跨文档推理 / 工具调用
Y 轴：0 到 5
三个柱子分别显示正确性、忠实度、完整性（分组柱状图）
颜色：三根柱子用强调色的三种不同明度，或者用强调色 + info + warning（你自己选，但要克制）

结论区（图表下方）：
一条信息卡，左侧 2px 竖线用强调色，背景 surface，内边距 12px
- 标题："分析结论"（13px，字重 500）
- 正文（13px，行高 22px）：
  "准确率从 run 3 的 50% 提升到 run 6 的 62.5%，提升主要来自重排环节。但跨文档推理这一类的忠实度只有 3.40，是最弱项。下钻到切片质量后发现，跨文档题目的命中片段平均长度 380 字，明显短于文档问答类的 620 字——问题出在分块策略，不在检索算法。建议调整 chunk_size 后重跑评测。"
- 底部一行：数据出处（12px，text-tertiary）："指标 eval_accuracy / eval_failures_by_category / chunk_quality · 查询耗时 298ms"

要区分两类数据来源的标签样式：
- 【SQL】→ 背景用 info 蓝的浅色、文字用 info 色
- 【Langfuse】→ 背景用强调色浅色、文字用强调色
标签尺寸要小（11px，高 18px），不要做成大色块。

示例提问列表（左侧输入框上方，可点击）：
- 最近三次评测的准确率趋势怎么样、哪个维度最弱？
- 最近一周延迟趋势、哪一段最慢？
- 哪些文档的处理失败了？
```

---

### P6 · 工具注册表 ToolsPage

> PRD 的 F9.8。这一页的价值在于它能让"工具来自哪里"这件事被看见。

```text
现在做工具注册表页面。它列出 Agent 当前可用的全部工具，并标明每个工具来自哪里。

页面结构：
1. 顶部工具栏
   - 左：标题"工具注册表" + 数字徽标
   - 右：一个刷新按钮（图标 ReloadOutlined）
2. 已连接的 Server 区（横向一排卡片）
3. 工具列表区（主要区域，Ant Design Table）

Server 卡片（每个宽 200px，高 88px）：
- 第一行：Server 名称（14px，字重 500）
- 第二行：状态 —— 8px 色点 + "已连接"，用 success 绿；未连接用 danger 红
- 第三行：小字（12px，text-tertiary）工具数 + 传输方式，例如 "11 个工具 · stdio"

渲染 3 个 Server 卡片：
- local       已连接  2 个工具 · 进程内
- inventory   已连接  1 个工具 · stdio
- harness     已连接  11 个工具 · stdio

工具表格（Ant Design Table，size="small"，必须用虚拟滚动或分页，因为工具可能很多）：
列定义：
- 工具名：12px 等宽字体。**注意命名空间前缀**——外部 Server 的工具名带 server 前缀（如 harness__list_pipelines），本地工具没有前缀（如 current_time）
- 来源：一个小标签。本地工具用中性灰标签，外部 Server 用强调色标签
- 描述：13px，单行省略
- 参数数量：等宽数字，右对齐，例如 "3 个"
- 操作：一个"展开"按钮（ChevronDownOutlined / ChevronUpOutlined）

展开行（Ant Design Table 的 expandedRowRender）：
显示该工具的 JSON Schema（从函数签名自动生成的那个），用等宽字体、sunken 背景、圆角 4px、内边距 12px，语法高亮可以不做但要保证缩进清晰。
例如：
{
  "type": "object",
  "properties": {
    "query":  { "type": "string", "description": "检索关键词" },
    "top_k":  { "type": "integer", "default": 5 }
  },
  "required": ["query"]
}

底部再加一个「调用记录」区块（可折叠，默认折叠）：
表格列：时间 | 工具名 | 耗时 | 结果 | 错误层级
错误层级这一列很重要，要区分两种情况（这是两种完全不同的错误）：
  - 工具层错误 → 标签写"工具返回错误"，用 warning 色
  - 协议层错误 → 标签写"协议错误"，用 danger 色
  - 正常 → 一个 success 色点 + "成功"

空态（当没有任何 Server 连接时）：
不要显示一个空表格。改为显示：
「还没有连接任何外部 Server。注册一个 →」
下面一行小字：可用 npx 启动标准 MCP Server

示例数据：

const mockServers = [
  { name: 'local',     connected: true,  tool_count: 2,  transport: 'in-process' },
  { name: 'inventory', connected: true,  tool_count: 1,  transport: 'stdio' },
  { name: 'harness',   connected: true,  tool_count: 11, transport: 'stdio' }
]

const mockTools = [
  { name: 'current_time',              source: 'local',     description: '返回当前时间，支持时区换算与时间差计算', param_count: 2 },
  { name: 'search_documents',          source: 'local',     description: '在知识库中检索相关文档片段',             param_count: 2 },
  { name: 'query_inventory',           source: 'inventory', description: '按条件查询库存记录',                     param_count: 3 },
  { name: 'harness__list_pipelines',   source: 'harness',   description: '列出项目下的所有流水线及其最近运行状态',   param_count: 3 },
  { name: 'harness__diagnose',         source: 'harness',   description: '诊断指定流水线失败的原因并给出建议',       param_count: 2 },
  { name: 'harness__list_executions',  source: 'harness',   description: '列出某条流水线的执行历史',               param_count: 4 },
  { name: 'harness__get_execution',    source: 'harness',   description: '获取单次执行的详细日志',                 param_count: 2 },
  { name: 'harness__list_repositories',source: 'harness',   description: '列出账号下的代码仓库',                   param_count: 2 },
  { name: 'harness__list_connectors',  source: 'harness',   description: '列出已配置的连接器',                     param_count: 2 },
  { name: 'harness__list_environments',source: 'harness',   description: '列出环境配置',                           param_count: 2 },
  { name: 'harness__list_services',    source: 'harness',   description: '列出服务定义',                           param_count: 2 },
  { name: 'harness__run_pipeline',     source: 'harness',   description: '触发一条流水线运行（写操作）',           param_count: 3 },
  { name: 'harness__list_secrets',     source: 'harness',   description: '列出密钥名称（不返回值）',               param_count: 2 },
  { name: 'harness__get_user',         source: 'harness',   description: '获取当前账号信息',                       param_count: 1 }
]

const mockCallLogs = [
  { time: '10:13:02', tool: 'harness__list_pipelines', latency_ms: 820,  result: 'ok',      error_layer: null },
  { time: '10:13:01', tool: 'search_documents',        latency_ms: 86,   result: 'ok',      error_layer: null },
  { time: '10:11:44', tool: 'query_inventory',         latency_ms: 12,   result: 'error',   error_layer: 'tool',    message: '缺少必填参数 filters' },
  { time: '10:11:30', tool: 'harness__run_pipeline',   latency_ms: 0,    result: 'blocked', error_layer: 'protocol', message: '写操作被 HARNESS_READ_ONLY=true 拦截' },
  { time: '10:09:18', tool: 'current_time',            latency_ms: 1,    result: 'ok',      error_layer: null }
]
```

---

### P7 · Trace 时间线（可选 · 最难的一页）

> ⚠️ **先说明一个事实**：这一页**不在 PRD 的功能清单里**（PRD 的 F8.3 是"用 Langfuse 自带面板看追踪"，不是自研页面）。它是我在 `DESIGN.md` §8.1 里追加的"加分项"。做不做由你决定——**做出来的话，它是面试时最能证明工程能力的一页。**
>
> 它也是 v0 最容易翻车的一页：Ant Design 里没有能用的现成组件（`Timeline` 是竖向的，形状完全不对），几乎全靠自绘。

```text
现在做 Trace 时间线页面。这一页展示一次 Agent 运行的内部步骤——把每个步骤画成一条横向的时间条。

重要前提：Ant Design 没有这个形状的组件，需要你用 div + 绝对定位自己画。请不要试图用 Timeline 组件。

页面结构：
1. 顶部工具栏
   - 左：trace 标题（14px）+ trace id（12px 等宽，text-tertiary）
   - 右：一个时间范围选择器 + 一个"只看失败"的开关
2. 概览条（一行，高 56px）
   - 显示 4 个数字：总耗时、span 总数、错误数、总 token
   - 数字 20px 等宽，标签 11px，text-tertiary
   - 4 个数字之间用 1px 竖线分隔（border-subtle）
3. 时间轴头部（刻度尺）
   - 一行刻度，从 0ms 到总耗时，均匀分布 6 个刻度
   - 刻度文字 11px，text-tertiary，等宽
   - 刻度线向下延伸 1px，颜色 border-subtle
4. Span 列表（主要区域）

每行 span 的结构（这是核心，请仔细实现）：
[名称区 260px] [时间条区 自适应] [耗时 80px]

名称区（从左到右）：
- 缩进表示层级：每一层缩进 12px，并在左侧画一条 1px 竖线（颜色 border-default）表示父子关系。不要用括号或其他符号
- 一个 8px 的状态色点：成功用 success 绿、失败用 danger 红、运行中用强调色（带脉冲动画）
- 名称（12px，等宽字体更清晰）
- 如果失败，整行最左侧再加一条 2px 的红色竖条

时间条区：
- 一根横条，位置和宽度按真实时间比例计算，不是等宽
- 条高 16px，圆角 2px
- 条形颜色：默认用 text-tertiary 的中性灰。不同类型用不同的中性色调：
  · LLM 调用 → 稍深的中性灰
  · 检索 → 稍浅的中性灰
  · 工具调用 → 中灰
  **注意：不要用彩色区分类型。彩色只留给状态（失败）。** 这是刻意的克制。
- 条内如果宽度够，显示一段 11px 的文字（token 数或摘要），颜色用白色或深色保证可读
- 失败的 span，条形用 danger 色的 20% 透明度填充 + 1px danger 描边

耗时列：
- 等宽字体，右对齐，格式 "1240ms" 或 "1.24s"
- 颜色 text-secondary

行高与密度：每行 32px，1440×900 视口下首屏必须能看到至少 12 行 span。这是硬指标。

悬停浮层：
- 鼠标悬停在 span 条上，显示一个浮层
- 浮层内容：span 名称、类型、开始时间、结束时间、耗时、token 数（如果有）、输入摘要、输出摘要
- 浮层样式：背景 surface-raised，1px 描边 border-default，单层阴影（0 4px 12px rgba(0,0,0,0.08)），圆角 6px，内边距 12px

点击 span：
- 从右侧滑出抽屉，显示该 span 的完整信息：完整的输入输出 JSON（等宽字体，sunken 背景）、模型参数、token 明细

示例数据（一次真实的 Agent 运行，7 个 span，其中 2 个有子 span）：

const mockTrace = {
  id: '7f3a9c2e1b8d4a6f',
  name: 'run_agent',
  total_ms: 3480,
  total_tokens: 4102,
  span_count: 9,
  error_count: 1
}

const mockSpans = [
  { id: 's1', parent: null, depth: 0, name: 'run_agent',        type: 'agent',  start_ms: 0,    duration_ms: 3480, status: 'error', tokens: 4102 },
  { id: 's2', parent: 's1', depth: 1, name: 'memory.load',      type: 'memory', start_ms: 12,   duration_ms: 45,   status: 'ok',    tokens: null },
  { id: 's3', parent: 's1', depth: 1, name: 'plan (llm)',       type: 'llm',    start_ms: 60,   duration_ms: 1120, status: 'ok',    tokens: 1840, model: 'deepseek-chat' },
  { id: 's4', parent: 's1', depth: 1, name: 'retrieve',         type: 'rag',    start_ms: 1185, duration_ms: 340,  status: 'ok',    tokens: null },
  { id: 's5', parent: 's4', depth: 2, name: '  bm25_search',    type: 'rag',    start_ms: 1188, duration_ms: 92,   status: 'ok',    tokens: null },
  { id: 's6', parent: 's4', depth: 2, name: '  vector_search',  type: 'rag',    start_ms: 1285, duration_ms: 168,  status: 'ok',    tokens: null },
  { id: 's7', parent: 's4', depth: 2, name: '  rerank',         type: 'rag',    start_ms: 1458, duration_ms: 62,   status: 'ok',    tokens: null },
  { id: 's8', parent: 's1', depth: 1, name: 'execute_tool',     type: 'tool',   start_ms: 1530, duration_ms: 820,  status: 'ok',    tokens: null, tool_name: 'harness__list_pipelines', server: 'harness' },
  { id: 's9', parent: 's1', depth: 1, name: 'plan (llm)',       type: 'llm',    start_ms: 2360, duration_ms: 1090, status: 'error', tokens: 2262, model: 'deepseek-chat', error: 'LLM 服务超时' }
]
```

---

## 3. 附录 A · 数据契约（**必须和页面块一起贴**）

> 这些是后端**已经实现**的接口返回形状（从 `app/schemas/` 逐个核对得来，不是编的）。给 v0 看这个，是为了让它生成的字段名和真实后端对得上，将来接真数据不用改结构。

```text
【后端已有的接口与响应形状（供你生成 mock 数据时对齐字段名）】

POST /api/chat                       非流式对话
  请求  { message: string, session_id?: string(uuid) }
  响应  {
    session_id: string,
    answer: string,                 可能含 [1] [2] 形式的引用编号
    sources: [ { index: number, source: string, content: string,
                 similarity: number|null, page_ref: string|null } ],
    invalid_citations: number[]     出现越界引用编号时非空
  }

GET /api/sessions                    会话列表（按最近活跃倒序）
  响应  [ { id: string(uuid), title: string, updated_at: string(ISO) } ]

GET /api/sessions/{id}/messages      会话历史（时间正序，含 role='tool' 的中间消息）
  响应  [ { role: 'user'|'assistant'|'tool', content: string,
             tool_calls: array|null, created_at: string(ISO) } ]

POST /api/documents                  multipart 上传，返回 202
  响应  { id: string(uuid), filename: string, file_type: 'pdf'|'docx'|'md'|'txt',
          status: 'processing'|'ready'|'failed', chunk_count: number,
          error_message: string|null, created_at: string(ISO) }

GET /api/documents                   文档列表（前端轮询它看 processing 变化）
  响应  同上结构的数组
DELETE /api/documents/{id}           204 No Content

GET /api/eval/cases                  评测集列表（**不含参考答案**）
  响应  [ { id: number, case_key: string, category: 'doc_qa'|'cross_doc'|'tool_call',
             difficulty: 'easy'|'medium'|'hard', is_negative: boolean, question: string } ]

GET /api/eval/cases/{case_key}       单条详情（含答案页）
  响应  同上 + { reference: string, evidence: string, doc_slugs: string[],
                 expected_tool: string|null }

GET /api/eval/dataset                评测集指纹与分布
  响应  { total: number, negative: number, by_category: {[k]: number},
          by_difficulty: {[k]: number}, covered_slugs: string[],
          fingerprint: string(sha256) }

GET /api/eval/runs                   运行列表（最新在前）
  响应  [ { id: number, config_name: string, dataset_fingerprint: string|null,
             corpus_fingerprint: string|null, judge_model: string|null,
             generation_runs: number|null, runs_per_case: number|null,
             score_correctness: number|null, score_faithfulness: number|null,
             score_completeness: number|null, accuracy: number|null,
             report_path: string|null, created_at: string(ISO) } ]

GET /api/eval/runs/{run_id}          运行详情（含按类别聚合）
  响应  同上 + {
    by_category: [ { category: string, rows: number, cases: number,
                     passed_rows: number, avg_correctness: number|null,
                     avg_faithfulness: number|null, avg_completeness: number|null } ],
    failure_breakdown: { [failure_reason]: number },
    rows: number, scored_rows: number, generation_inconsistent: number
  }

GET /api/eval/runs/{run_id}/cases    明细（可按 category / passed 过滤）
  响应  [ { id: number, case_key: string, category: string,
             generation_index: number, answer: string,
             tool_calls: string[]|null, sources_count: number,
             score_correctness: number|null, score_faithfulness: number|null,
             score_completeness: number|null, judge_runs: number,
             passed: boolean, failure_reason: string|null,
             failure_reason_label: string } ]

GET /api/eval/runs/{run_id}/results/{case_key}
                                     某题的全部生成（含检索片段）
  响应  同上 + { retrieved_chunks: string, judge_raw: array|null }

GET /api/tools                       全部已注册工具
  响应  {
    total: number,
    by_source: { [source]: string[] },
    servers: [ { name: string, connected: boolean, tool_count: number, ... } ],
    tools: [ { name: string, description: string,
               parameters: object, source: string } ]
  }

GET /api/health
  响应  { status: 'ok'|'degraded', version: string,
          deps: { api: 'ok', redis: 'ok'|'down' } }

【两个必须知道的口径（生成表格时用得上）】
1. 三维度均分（正确性/忠实度/完整性）的分母是"有分数的明细行数"，不是题目总数。
   工具调用类题目不送 judge 判内容，得分为 null，所以分母一定小于总行数。
2. accuracy（准确率）的分母是"题目数"——同一题多次生成先取多数结论。
   这两个口径不可互证，界面上同时出现时必须分别标注。
```

---

## 4. 附录 B · AntD 主题配置（贴给 v0 当参考代码）

> 这段是 `DESIGN.md` §3.6 的配置，`components` 里的 token 名是**逐个核对 AntD 源码的 `ComponentToken` 定义**得来的。直接贴给 v0，它能少猜很多东西。

```ts
// src/theme/antdTheme.ts
import type { ThemeConfig } from 'antd';

const fontSans =
  "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', " +
  "'Microsoft YaHei', system-ui, sans-serif";
const fontMono =
  "'SF Mono', 'JetBrains Mono', Menlo, Consolas, 'Noto Sans Mono CJK SC', monospace";

export const lightTheme: ThemeConfig = {
  token: {
    colorPrimary:  '#0E6F63',
    colorLink:     '#0E6F63',
    colorSuccess:  '#0B7A46',
    colorWarning:  '#8A5A08',
    colorError:    '#C0271D',
    colorInfo:     '#0B6BA8',

    colorBgLayout:        '#F5F6F8',
    colorBgContainer:     '#FFFFFF',
    colorBgElevated:      '#FFFFFF',
    colorBorder:          '#D5D9DF',
    colorBorderSecondary: '#E4E7EB',

    colorText:            '#14181E',
    colorTextSecondary:   '#4A5361',
    colorTextTertiary:    '#667080',
    colorTextQuaternary:  '#949CA8',
    colorTextDisabled:    '#949CA8',
    colorTextPlaceholder: '#667080',

    fontSize:      13,
    fontSizeSM:    11,
    fontSizeLG:    15,
    controlHeight: 32,
    controlHeightSM: 26,
    controlHeightLG: 36,
    borderRadius:   6,
    borderRadiusSM: 4,
    borderRadiusLG: 6,      // AntD 默认 8，必须改
    borderRadiusXS: 2,
    lineHeight:     1.54,

    fontWeightStrong: 500,  // 默认 600

    boxShadow:          '0 4px 12px rgb(0 0 0 / 0.08)',
    boxShadowSecondary: '0 4px 12px rgb(0 0 0 / 0.08)',
    boxShadowTertiary:  '0 1px 2px rgb(0 0 0 / 0.04)',

    fontFamily:     fontSans,
    fontFamilyCode: fontMono,

    lineWidthFocus:      2,
    controlOutlineWidth: 2,
  },
  components: {
    Layout: {
      headerBg:     '#FFFFFF',  // 默认 '#001529' 深蓝黑，必须改
      siderBg:      '#FFFFFF',  // 默认 '#001529'
      bodyBg:       '#F5F6F8',
      headerHeight: 48,         // 默认 64
      headerPadding: '0 16px',
    },
    Table: {
      headerBg:         '#F5F6F8',
      headerColor:      '#4A5361',
      headerSplitColor: 'transparent',
      borderColor:      '#E4E7EB',
      rowHoverBg:       '#F5F6F8',
      cellFontSize:     13,
      cellFontSizeMD:   13,
      cellFontSizeSM:   12,
      cellPaddingBlock:    6,   // 默认 16 → 行高从约 54px 压到 32px
      cellPaddingInline:   12,  // 默认 16
      cellPaddingBlockMD:  4,
      cellPaddingInlineMD: 8,
      cellPaddingBlockSM:  2,
      cellPaddingInlineSM: 8,
    },
  },
};

// 深色主题：结构完全相同，把色值换成下面这套
const darkColors = {
  colorPrimary:  '#4FD1B8',
  colorSuccess:  '#3DD68C',
  colorWarning:  '#E0A83A',
  colorError:    '#FF7A6E',
  colorInfo:     '#5FB4E8',
  colorBgLayout: '#131519',
  colorBgContainer: '#191C21',
  colorBgElevated:  '#1F2329',
  colorBorder:          '#33383F',
  colorBorderSecondary: '#2A2F36',
  colorText:          '#F2F3F5',
  colorTextSecondary: '#A8B0BC',
  colorTextTertiary:  '#7B8494',
  colorTextQuaternary:'#5A626E',
  colorTextDisabled:  '#5A626E',
  colorTextPlaceholder:'#7B8494',
  boxShadow:          '0 4px 12px rgb(0 0 0 / 0.4)',
  boxShadowSecondary: '0 4px 12px rgb(0 0 0 / 0.4)',
  boxShadowTertiary:  '0 1px 2px rgb(0 0 0 / 0.24)',
};
// export const darkTheme: ThemeConfig = { token: { ...同结构, ...darkColors }, components: {...} }
```

**用法**：

```tsx
import { ConfigProvider, theme as antdTheme } from 'antd';
import { lightTheme, darkTheme } from './theme/antdTheme';

<ConfigProvider theme={isDark ? darkTheme : lightTheme}>
  <App />
</ConfigProvider>
```

---

## 5. 生成完之后你要做的事（**不要粘给 v0**）

1. **人工审查**：v0 大概率会混进 shadcn 的写法或漏掉某些 token 覆盖。逐个文件看一遍，重点看：有没有出现裸色值（`#fff`、`color: 'red'`）、有没有用 Tailwind 类名、有没有绕开 ThemeConfig 写局部的 `<ConfigProvider>`。
2. **搬进 Vite 项目**：v0 的预览是 Next.js 环境，把 `src/pages/` 和 `src/components/` 的文件移过去，去掉 Next.js 专属 import。
3. **换掉 mock 数据**：按附录 A 的真实接口逐个替换 `fetch`。注意后端目前的 `/api/chat` 是**非流式**的（SSE 还没实现），所以对话台的打字机效果需要等后端上了 SSE 才能真跑——**界面可以先做好**。
4. **核对组件 token**：`node_modules/antd/es/table/style/index.d.ts` 和 `.../layout/style/index.d.ts` 里的 `ComponentToken` 接口，确认你装的 AntD 版本里字段名没变。
5. **接 ECharts 主题**：图表的色值必须浅深两套显式传进 `option`，切换主题时重建实例。

---

## 附录 · 本文件与其它文档的关系

| 文档 | 管什么 | 冲突时怎么办 |
|---|---|---|
| `PRD-v4.1-含前沿技术.md` | 功能范围（要做什么） | **功能听 PRD** |
| `DESIGN.md` | 视觉规范（长什么样） | **视觉听 DESIGN** |
| 本文件 | 把上面两者翻译成 v0 能吃的提示词 | 从属，不引入新决策 |

**一处已知出入（如实记录）**：P7 Trace 时间线页**不在 PRD 的功能清单内**——PRD 的 F8.3 是"用 Langfuse 自带面板看追踪"。这一页是 `DESIGN.md` §8.1 追加的加分项，做不做由你定。
