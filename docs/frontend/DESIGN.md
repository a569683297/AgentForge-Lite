# AgentForge-Lite 前端设计规范（DESIGN.md）

> **这份文件是干什么的**：把"审美"翻译成 AI 和人都能执行的**硬约束**。
> 每次让 AI 生成界面前，把这份文件 + 参考图一起丢给它。不给这份文件，AI 就只能吐训练数据的平均值 —— 那是"AI 感"的唯一来源。
>
> 版本 **v1.1** · 2026-10-03
> 状态：强调色 ✅ 已定（青）· 组件库 ✅ 已定（**保留 PRD 的 AntD**）· 参考图 ⏳ 待收集
>
> **v1.0 → v1.1 变更**：① 强调色定案（青）；② 组件库由 Tailwind + shadcn/ui 改为 **AntD**（遵从 PRD 原选型）；③ 新增 §3.6 AntD 主题映射（可直接粘贴的 `ThemeConfig`）；④ 新增 §1.3 中"AntD 中后台感"这条反面参照；⑤ §9 按 AntD 重写。

---

## 0. 先统一几个词（本文出现的专业名词在这里一次说清）

- **设计 token（设计令牌）**：一个带名字的样式最小单位，比如"正文颜色 = #14181E"。意思是这个决定**只在一个地方定义**，全站引用它。好处：改一个值全站跟着变；也让 AI 无法"自己挑一个蓝色"。
- **canvas / surface / sunken**：三个层级的中性底。"canvas"是页面最底那层，"surface"是浮在上面的卡片，"sunken"是凹进去的区域（代码块、时间线轨道）。
- **span（跨度 / 调用片段）**：一次 Agent 运行会被拆成很多步骤，每个步骤（一次 LLM 调用、一次检索、一次工具调用）叫一个 span。Trace 页的主体就是"把一屏 span 按时间画成横条"。
- **tabular-nums**：一种字体的数字排版开关。打开后每个数字占的宽度相同，数字就能上下对齐。关着的时候 `1111` 比 `8888` 窄，表格里的数字会参差不齐。
- **骨架屏（skeleton）**：加载时先画出内容的灰色轮廓，让人知道"这里将出现什么"，而不是转圈或写 `Loading...`。
- **WCAG AA**：国际无障碍规范里最常见的那一档要求，正文文字与背景的对比度至少 **4.5 : 1**。低于这个值，弱视和低质量屏幕上会看不清。
- **对比度（contrast ratio）**：两个颜色的明暗差，范围 1:1（完全相同）到 21:1（纯黑对纯白）。本文所有数值都是脚本实算的，不是估的。
- **参考图（reference screenshot）**：别人**真实产品**的界面截图，用来当"标尺"。**注意是截图，不是设计概念图**。它解决什么问题、怎么收集，见 §1.2 —— 这是本文件最容易被误解的一节。
- **ConfigProvider**：AntD 提供的一个 React 组件，用 `<ConfigProvider theme={...}>` 包住整个应用，主题配置从这一个地方生效。**本文 §3.6 的所有 token 都写在这个组件里。**
- **Seed / Map / Alias Token（种子 / 梯度 / 别名令牌）**：AntD 主题的三层派生结构。**Seed Token** 是源头（比如 `colorPrimary`），AntD 用算法自动推出整条色阶；**Map Token** 是派生出来的梯度值（比如 `colorPrimaryHover`、`colorBgContainer`）；**Alias Token** 是语义别名（比如 `colorTextDisabled`、`colorTextPlaceholder`）。三层是**自动派生**关系 —— 改 Seed 会连锁改 Map 和 Alias。理解这一点才能预判"改一个值会连带改什么"。
- **Component Token（组件令牌）**：只作用于某一个组件的 token，写在 `<ConfigProvider theme={{ components: { Table: {...} } }}>` 里，可以覆盖该组件消费的 Alias Token。**本文 §3.6 的 Table / Layout 部分就是它。**

---

## 1. 设计定位

### 1.1 一句话

> **这是一台机器的仪表盘，不是一张宣传单。**

产品定位决定审美基准。AgentForge-Lite 的用户是"要看 Agent 到底干了什么"的工程师，不是来被说服购买的人。所以：

- 优先**信息密度**，不是呼吸感
- 优先**可比对的结构**，不是视觉惊喜
- 优先**真实状态**（跑着 / 卡住 / 失败 / 空了），不是永远好看的 demo 态

### 1.2 参考图是什么、为什么要它、怎么收集

**先说它不是什么**：不是让你去 Dribbble / Behance 找那种"概念设计稿"，也不是让你做设计。**它就是截图**——打开几个成熟产品，把它们的实际界面截下来，存进项目里。

**为什么要它——两个作用，第二个才是主要的：**

| 作用 | 说明 |
|---|---|
| ① **喂给 AI**（次要） | 给 AI 一段文字描述，它只能吐训练数据的平均值；给它一张真实截图说"照这个密度和层次来"，它才有落点。（v0 官方文档明确支持"从截图克隆页面"，这条已确证） |
| ② **给你自己当标尺**（主要） | 本文写了"密度优先""层级用描边不用阴影"，但这些话是**虚的**。手边有一张 Linear 的图，你一眼就能看出自己搭的界面差在哪 —— 是留白太多、还是行高太松、还是颜色太跳。**没有标尺，"密度不够"永远是个说不清的判断。** |

> ⚠ **这一条在你改用 AntD 之后更重要了。**
> 因为 v0 / Stitch 直出的代码是 shadcn + Tailwind，和 AntD 不同源，代码基本不能直接落库。也就是说 **AI 帮你写代码这条路变窄了，参考图的作用从"给 AI 的输入"更多地变成了"给你自己的标尺"**。你自己照着搭，标尺必须有。

**存哪、怎么命名**

存 `AgentForge-Lite/docs/frontend/refs/`，命名 `来源-页面-日期.png`：

```
refs/
├── langfuse-trace-detail-2026-10-03.png
├── linear-issue-list-2026-10-03.png
├── vercel-project-overview-2026-10-03.png
├── datadog-trace-waterfall-2026-10-03.png
└── dify-workflow-canvas-2026-10-03.png
```

**收哪五张**（按下面的清单去开页面截图即可，不需要注册账号）：

| # | 来源 | 截什么 |
|---|---|---|
| 1 | Langfuse 官网 / 公开 demo | trace 详情页 —— **它和我们第 8.1 节要做的页是同一类东西** |
| 2 | Linear 官网 / 文档页 | 问题列表页 —— 看列表密度和"近乎无色的骨架" |
| 3 | Vercel 官网 Dashboard 截图 | 项目概览 —— 看层级怎么用描边表达 |
| 4 | Datadog APM 文档页的截图 | trace waterfall —— 看横向 span 图的比例感 |
| 5 | 本机 Dify（`~/dify` 已跑着） | 工作流画布 —— 看节点连线和运行状态着色 |

**⚠ 关于"够不够、急不急"—— 这条我写错过一次，2026-10-03 用户追问后修正**

v1.1 初稿在这里写的是「**五张没集齐，不要开始搭界面**」。**这条定得太硬，不成立**，已撤回。准确表述：

| 说法 | 成立吗 | 为什么 |
|---|---|---|
| "没有参考图就不能开工" | ❌ **不成立** | 参考图影响的是**质量上限，不是能否开工**。它**从来不是"给 AI 的输入条件"**——代码任何时候都能写 |
| "搭某一页之前，手边至少有 **1 张**同类产品的截图" | ✅ **成立** | 搭 Trace 页之前，手边有 1 张 Langfuse / Datadog 的 trace 截图就够了。**不需要凑齐 5 张** |

剩下的图**边做边补**：搭到某一页觉得"哪里不对"时再去截那一张，效率反而更高——因为那时你已经知道该看什么了。

→ **这条修正也顺带说明一个更大的原则：参考图是给"你自己"的标尺，不是给 AI 的投喂材料。** 既然不是投喂材料，它就不该成为开工的硬门槛。

### 1.3 反面参照（明确不要）

**A. 不要 AI 默认输出那套（shadcn / Tailwind landing page 长相）**

大圆角、大留白、居中大标题、三张 Feature 卡片并排、渐变 hero。这套长相的来源是 AI 训练数据的平均值，跟你是谁无关。

**B. 不要 AntD Pro 那套中后台模板感（这条是改用 AntD 之后的新增重点）**

这不是"AntD 不好"，是 AntD 的**出厂默认值**给你的两个包袱，必须主动去掉：

| 包袱 | 出厂默认值 | 为什么难看 |
|---|---|---|
| **深蓝黑顶栏 + 侧边栏** | `Layout.headerBg = #001529`、`Layout.siderBg = #001529` | 这是"后台管理系统"最典型的视觉签名，2015 年至今没变过 |
| **默认蓝色** | `colorPrimary = #1677ff` | 全网 AntD 系统都是这个蓝 |
| **过宽的单元格内边距** | `Table.cellPaddingBlock = 16px` | 算下来单行高约 **54px**，一屏放不下 15 行 |
| **64px 的顶栏** | `Layout.headerHeight = controlHeight × 2 = 64` | 落地页高度，不是工具的高度 |
| 三层堆叠阴影 | `boxShadow` 默认三层叠加 | 浮层看着"糊" |

> 这几条我都是从 AntD 源码里读的 `prepareComponentToken` 默认值，不是凭印象 —— 见 §3.6 的注释。
> **结论：AntD 的问题不在组件，在默认值。默认值在 §3.6 里全部改掉，AntD 就不会有 ERP 感。**

**C. 不要 Dify 的装饰性渐变**

我读过它的 `themes/manual-light.css`，那个文件里几乎每一条都是 `linear-gradient`（玻璃拟态、发光描边、渐变遮罩）。这是"产品营销感"，不是"工程感"—— 别把"参考 Dify"理解成"抄它的视觉"。

**D. 但 Dify 的 token 结构值得抄**

它的 `packages/dify-ui/src/themes/light.css`（800 行）用的是 `text / background / state / components / divider / shadow` 六段式命名 + 15 条色阶 × 7 档。而且文件头写着 `/* Attention: Generate by code. Don't update by hand!!! */` —— **设计 token 是生成的资产，不是手写维护的东西**。这条纪律直接照搬，本文 §3.6 就是这么做的。

---

## 2. 十条硬规则（"不许"清单）

这一节是整份文件里最值钱的部分。有开发者实测：光是给 AI 一份明确的禁止清单，就能砍掉大约八成通用输出。

| # | 规则 | 为什么 |
|---|---|---|
| **R01** | **先密度，后留白。** 一屏（1440×900）至少 20 行有效信息；列表行高上限 36px | AI 默认吐 48px+ 行高的"透气"版式，那是落地页的密度 |
| **R02** | **层级用「背景微差 + 1px 描边」表达，不用阴影** | 阴影是 AI 生成界面最显眼的同质化特征 |
| **R03** | **圆角只有两档：容器 6px、控件 4px。禁止 ≥ 12px 的圆角，禁止胶囊按钮** | 8/12/16px 圆角 + 胶囊是 shadcn 默认长相 |
| **R04** | **强调色像素面积 ≤ 全屏 3%。** 强调色只给"当前选中 / 链接 / 焦点环 / 主按钮" | 大色块是"AI 感"的第二来源 |
| **R05** | **所有数字用等宽字体 + `tabular-nums`，表格里右对齐** | 耗时、token 数、分数必须能竖着比 |
| **R06** | **状态用 8px 色点 + 文字表达，禁止用整块彩色背景** | 彩色背景块是"AI 生成 dashboard"的典型脸 |
| **R07** | **禁止渐变。** 唯一例外：流式输出的光标与进度条 | 渐变 hero 是 AI 感的第一大来源 |
| **R08** | **禁止用 emoji 当图标**（AntD 项目请用 `@ant-design/icons`，且同层级图标尺寸统一） | 一看就是生成的 |
| **R09** | **空 / 加载 / 错误三态必须真实设计**，禁止 `Loading...`、禁止空白页 | AI 只给你"数据齐全时好看"的那一屏 |
| **R10** | **禁止 hero 结构**（居中大标题 + 副标题 + 两个按钮） | 你不是在卖东西 |

---

## 3. 色彩

### 3.1 设计决策

**刻意避开三类"AI 默认色"**：

| 避开 | 为什么 |
|---|---|
| Tailwind `blue-600 #2563EB` | v0 / shadcn 的默认 primary，撞车率最高 |
| `indigo-500 #6366F1` 与一切紫蓝渐变 | "AI 生成"的视觉签名 |
| **AntD `#1677FF`** | 全网 AntD 系统的默认主色，用它就是"没改过主题" |
| Dify 电光蓝 `#0033FF` | 抄了就是"仿 Dify" |

**主色方案：近乎无色的石墨骨架 + 一个克制的强调色。**

骨架用中性灰阶（带极轻微冷调，不是纯灰），交互焦点才给颜色。这是 Linear / Vercel 的做法——整个界面看起来是灰的，但一点都不单调。

### ✅ 强调色（已定）

**青 `#0E6F63`（Light）/ `#4FD1B8`（Dark）** —— 2026-10-03 拍板，理由与取舍见 §3.4。

### 3.2 色板（Light / Dark）

#### 中性层

| Token | Light | Dark | 用途 |
|---|---|---|---|
| `canvas` | `#F5F6F8` | `#131519` | 页面最底层 |
| `surface` | `#FFFFFF` | `#191C21` | 卡片、面板 |
| `surface-raised` | `#FFFFFF` | `#1F2329` | 浮层（modal / dropdown） |
| `sunken` | `#ECEEF1` | `#0E1013` | 凹入区：代码块、时间线轨道、输入框底 |
| `border-subtle` | `#E4E7EB` | `#2A2F36` | 分隔线 |
| `border-default` | `#D5D9DF` | `#33383F` | 卡片 / 控件描边 |
| `border-strong` | `#B8BEC7` | `#454B54` | hover / 聚焦描边 |

#### 文字层

| Token | Light | Dark | 对比度（实测） | 用途 |
|---|---|---|---|---|
| `text-primary` | `#14181E` | `#F2F3F5` | Light 17.8 : 1 · Dark 15.4 : 1 | 正文、标题 |
| `text-secondary` | `#4A5361` | `#A8B0BC` | 7.8 : 1 · 7.8 : 1 | 次要说明、表头 |
| `text-tertiary` | `#667080` | `#7B8494` | 5.0 : 1 · 4.5 : 1 | 元信息、时间戳 |
| `text-disabled` | `#949CA8` | `#5A626E` | 2.8 : 1 · 2.8 : 1 | **仅限禁用态**，见下方规则 |

> ⚠ **关于禁用态与占位符**：`text-disabled` 实测只有 2.8 : 1，**不达 AA**。这是有意的取舍——禁用态本来就该"看起来不能点"。
> **但它带来一条硬规则：任何承载信息的文字，一律禁止使用 `text-disabled`。**
> 同样地，**输入框的 placeholder（占位提示）用 `text-tertiary`，不要为了"看着像占位符"把对比度压到 3 以下** —— 靠字号或斜体区分，不靠降低可读性。
> §3.6 里 `colorTextPlaceholder` 被显式设成 `#667080` 而不是 AntD 默认的 `rgba(0,0,0,0.25)`，原因就是这条。

#### 语义色

| Token | Light | Dark | 对比度（Light / Dark） |
|---|---|---|---|
| `accent` | `#0E6F63` | `#4FD1B8` | 6.05 : 1 · 9.07 : 1 |
| `on-accent` | `#FFFFFF` | `#0A2A24` | 6.05 : 1 · 8.14 : 1 |
| `success` | `#0B7A46` | `#3DD68C` | 5.40 : 1 · 9.11 : 1 |
| `danger` | `#C0271D` | `#FF7A6E` | 5.92 : 1 · 6.72 : 1 |
| `warning` | `#8A5A08` | `#E0A83A` | 5.92 : 1 · 8.00 : 1 |
| `info` | `#0B6BA8` | `#5FB4E8` | 5.70 : 1 · 7.47 : 1 |

**全部通过 WCAG AA（≥ 4.5 : 1）。** 上表每个数字都是脚本实算的，不是估的。

### 3.3 语义色使用纪律

1. **评测看板的"通过 / 失败"用 `success` 绿 / `danger` 红** —— 这是国际开发者工具的通用惯例（JUnit、CI、Grafana 一律如此）。**注意：这与"中国股市红涨绿跌"完全不冲突，两者是不同语境**，不要混。
2. **`accent`（青）只用于交互**：当前选中项、链接、焦点环、主按钮。**永不用于表达状态**。
3. **`success`（绿）只用于状态**，永不用于交互。
   → 这条纪律是必需的，因为青与绿在同一色相区。**同一条信息里不得同时出现 accent 与 success**，否则用户分不清"这是我能点的"还是"这是已经好的"。
4. 语义色**只用色点 + 文字**，禁止铺成色块底（R06）。

### 3.4 强调色定案记录

2026-10-03 拍板：**方案 A · 青**。

| 方案 | 值 | 结论 |
|---|---|---|
| **A · 青** | `#0E6F63` / `#4FD1B8` | ✅ **采用**。与红/橙/蓝三个状态色都有足够距离；工程感强；暗色主题下非常干净。**已知取舍**：与 `success` 绿同色相区，靠 §3.3 的纪律隔离 |
| B · 暖橙 | `#B4530A` / `#F0A24B` | ❌ 落选。辨识度更高，但与 `warning` 琥珀语义打架；一个界面里同时出现"运行橙 + 警告黄"会很吵 |

要改的话改 §3.6 里的 `colorPrimary` / `colorLink` 两行即可（**同时要改 CSS 变量，见 §3.5 的单一来源要求**）。

### 3.5 CSS 变量（AntD 覆盖不到的地方用）

> **定位变了（v1.1 重要变更）**：v1.0 里这套变量是"全站唯一色彩来源"。改用 AntD 后，**AntD 组件自己走的是 JS token 体系（§3.6），不走这套 CSS 变量**。
> 所以这里保留它，但用途收窄为三处：
> 1. **自绘区域** —— 比如 §8.1 的 Trace 横向 span 图（AntD 里没有这个组件，得自己画）
> 2. **图表库配色** —— ECharts 等库要显式传色值，不认 AntD 的 token
> 3. **全局背景/文字兜底** —— `<body>` 这个层级 AntD 管不到

主题切换用 **`<html data-theme='...'>` 属性**，不要只依赖 `prefers-color-scheme`（系统偏好媒体查询只反映系统设置，用户没法在应用内手动切）。

```css
/* tokens.css — 自绘区域与图表的色彩来源。
   注意：这里和 §3.6 是同一份色板的两处落地，改色必须两处一起改（见下方"单一来源"） */

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
  --af-on-accent: #FFFFFF;

  --af-success: #0B7A46;
  --af-danger: #C0271D;
  --af-warning: #8A5A08;
  --af-info: #0B6BA8;

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
  --af-on-accent: #0A2A24;

  --af-success: #3DD68C;
  --af-danger: #FF7A6E;
  --af-warning: #E0A83A;
  --af-info: #5FB4E8;

  --af-shadow-overlay: 0 4px 12px rgb(0 0 0 / 0.4);
}
```

**★ 单一来源要求（避免同一个色写两遍写歪）**

色板现在有两处落地：§3.5 的 CSS 变量 和 §3.6 的 AntD token。**不要手抄两份**，正确做法是：

```ts
// src/theme/palette.ts —— 色板的唯一真源
export const palette = {
  light: { accent: '#0E6F63', success: '#0B7A46', /* ... */ },
  dark:  { accent: '#4FD1B8', success: '#3DD68C', /* ... */ },
} as const;
```

然后 `antdTheme.ts` 和注入 CSS 变量的那段代码**都从这个文件读**。改色只改这一处。

> **另一条更省事的路（推荐先试）**：AntD 从 v5 起支持 `theme={{ cssVar: true }}`，开启后它会把**自己算好的** token 输出成 CSS 变量（前缀默认 `ant`），变量名形如 `--ant-color-primary`。
> 好处是彻底消灭"两处落地"的问题 —— 图表和自绘区域直接读 `--ant-color-primary` 就行。
> ⚠ **但变量名的确切格式我没实跑验证过**，属于推测。落地第一步请先在浏览器 DevTools 里执行 `[...document.styleSheets]` 或直接看 `<html>` 元素的 `--ant-*` 变量，**确认命名规则后再决定用哪种方案**。

---

### 3.6 ★ AntD 主题映射（`ThemeConfig`，可直接粘贴）

**这是 v1.1 最核心的新增**：把 §3.2 的色板和 §5 的密度规则，翻译成 AntD 能执行的 `theme` 对象。

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
    colorSuccess:  palette.light.success,  // #0B7A46
    colorWarning:  palette.light.warning,  // #8A5A08
    colorError:    palette.light.danger,   // #C0271D
    colorInfo:     palette.light.info,     // #0B6BA8

    // ── 中性底（Map：必须显式覆盖，否则继承默认灰 #f5f5f5）──
    colorBgLayout:        palette.light.canvas,        // #F5F6F8
    colorBgContainer:     palette.light.surface,       // #FFFFFF
    colorBgElevated:      palette.light.surfaceRaised, // #FFFFFF
    colorBorder:          palette.light.borderDefault, // #D5D9DF  默认 #d9d9d9
    colorBorderSecondary: palette.light.borderSubtle,  // #E4E7EB  默认 #f0f0f0

    // ── 文字（Alias）─────────────────────────────────
    // ⚠ 这里故意用「实色」而不是 AntD 默认的 rgba(0,0,0,.88) 那套透明黑。
    //   原因：透明黑叠在 canvas 上和在 surface 上会呈现两种不同的灰，
    //   导致「同一个 text-secondary 在两处看起来不一样」。我们用固定色阶换掉它。
    colorText:            palette.light.textPrimary,
    colorTextSecondary:   palette.light.textSecondary,
    colorTextTertiary:    palette.light.textTertiary,
    colorTextQuaternary:  palette.light.textDisabled,
    colorTextDisabled:    palette.light.textDisabled,
    colorTextPlaceholder: palette.light.textTertiary,   // ← 默认 0.25 黑不达 AA，见 §3.2

    // ── 密度（Seed + Map）—— 这是和 AntD 出厂值差别最大的一段 ──
    fontSize:      13,   // 默认 14        ← §5 基准字号
    fontSizeSM:    11,   // 默认 12
    fontSizeLG:    15,   // 默认 16
    controlHeight: 32,   // 默认 32  ✅ 天然一致，AntD 这点比 shadcn 好
    controlHeightSM: 26, // 默认 24
    controlHeightLG: 36, // 默认 40
    borderRadius:   6,   // 默认 6   ✅ 一致
    borderRadiusSM: 4,   // 默认 4   ✅ 一致（控件档）
    borderRadiusLG: 6,   // 默认 8   ← 必须改！Card / Modal 走这个
    borderRadiusXS: 2,   // 默认 2
    lineHeight:     1.54,// 默认 1.5714；13px × 1.54 ≈ 20px 行高，见 §5.2

    // ── 字重：AntD 默认 600，本规范只要 400 / 500（§4.2）──
    fontWeightStrong: 500, // 默认 600

    // ── 阴影：压掉 AntD 默认的三层堆叠，只留浮层单层（R02）──
    boxShadow:          `0 4px 12px rgb(0 0 0 / 0.08)`,
    boxShadowSecondary: `0 4px 12px rgb(0 0 0 / 0.08)`,
    boxShadowTertiary:  `0 1px 2px rgb(0 0 0 / 0.04)`,

    // ── 字体 ─────────────────────────────────────────
    fontFamily:     fontSans,   // AntD 默认不含 'PingFang SC'，中文字形会飘
    fontFamilyCode: fontMono,

    // ── 焦点环（§6.3）─────────────────────────────────
    lineWidthFocus:      2,
    controlOutlineWidth: 2,
  },

  components: {
    // ★ Layout：AntD 中后台感的最大来源就是这两个 #001529
    Layout: {
      headerBg:  palette.light.surface,   // 默认 '#001529' ← 深蓝黑顶栏，必须改
      siderBg:   palette.light.surface,   // 默认 '#001529'
      bodyBg:    palette.light.canvas,    // 默认 colorBgLayout
      headerHeight: 48,                   // 默认 controlHeight×2 = 64 ← 落地页高度
      headerPadding: '0 16px',            // 默认 controlHeightLG×1.25 = 50px
    },

    // ★ Table：行高从默认约 54px 压到 32px 的地方
    Table: {
      headerBg:         palette.light.canvas,          // 默认 colorFillAlterSolid
      headerColor:      palette.light.textSecondary,   // 表头用次级文字，不用主文字
      headerSplitColor: 'transparent',                 // 去掉表头竖分隔线
      borderColor:      palette.light.borderSubtle,    // 默认 colorBorderSecondary
      rowHoverBg:       palette.light.canvas,
      cellFontSize:     13,  // 默认 fontSize
      cellFontSizeMD:   13,
      cellFontSizeSM:   12,
      // 行高算法：cellPaddingBlock×2 + lineHeight(20) = 目标行高
      //   目标 32px → cellPaddingBlock = 6       （AntD 默认 16 → 行高约 54px）
      //   目标 28px → cellPaddingBlock = 4
      cellPaddingBlock:    6,
      cellPaddingInline:   12,   // 默认 16
      cellPaddingBlockMD:  4,    // Table size="middle"
      cellPaddingInlineMD: 8,
      cellPaddingBlockSM:  2,    // Table size="small"
      cellPaddingInlineSM: 8,
    },
  },
};

// 深色主题同结构，色值取自 palette.dark
export const darkTheme: ThemeConfig = {
  token: { /* ... 同上结构，色值换 palette.dark ... */ },
  components: { /* ... 同结构 ... */ },
};
```

**用法**：

```tsx
import { ConfigProvider, theme as antdTheme } from 'antd';
import { lightTheme, darkTheme } from './theme/antdTheme';

<ConfigProvider
  theme={isDark ? darkTheme : lightTheme}
  cssVar                       // 可选，见 §3.5 末尾
>
  <App />
</ConfigProvider>
```

> **关于 `components` 里的 token 名**：上表的 `Layout` 与 `Table` 部分，是我从 AntD 源码的 `ComponentToken` 接口定义里逐个核对出来的，**默认值也来自源码里的 `prepareComponentToken`**，不是凭印象。
> **但组件级 token 的名称会随 AntD 版本变动。** 落代码前花一分钟核对：
> ```bash
> cat node_modules/antd/es/table/style/index.d.ts   # 找 ComponentToken
> cat node_modules/antd/es/layout/style/index.d.ts
> ```
> 以你实际安装的版本为准。
>
> **本文档只列了 Layout / Table 两个组件。** 其他组件（Button / Card / Select / Tag / Statistic…）**按需扩写**，扩写时遵守同一条原则：**先查该组件的 `ComponentToken`，只改默认值里有问题的项，不要为了"统一"而全量覆盖**（全量覆盖会让你升级 AntD 时错过它的修复）。

---

## 4. 字体

### 4.1 字体栈

```css
--af-font-sans: -apple-system, BlinkMacSystemFont, 'SF Pro Text',
                'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
--af-font-mono: 'SF Mono', 'JetBrains Mono', Menlo, Consolas,
                'Noto Sans Mono CJK SC', monospace;
```

（AntD 侧对应 `token.fontFamily` / `token.fontFamilyCode`，已在 §3.6 设置。）

**不要引入 Inter。** Inter 本身没问题，但它已经是 v0 / shadcn 的事实默认字体——用它等于自动加入平均值。系统字体栈在你用户的机器上天然无特征、零加载成本、渲染最快。

> **AntD 默认字体栈里没有 `'PingFang SC'`。** 在 macOS 上 `-apple-system` 会回退到苹方，看着没问题；但在没配好中文回退的环境里，中文字形会跳到别的字体上去。所以 §3.6 显式覆盖了 `fontFamily`。

### 4.2 字号与字重

| 角色 | 字号 / 行高 | 字重 | AntD 对应 token |
|---|---|---|---|
| 页面标题 h1 | 20 / 28 | 500 | `fontSizeHeading4`(20) 或自定义 |
| 区块标题 h2 | 16 / 24 | 500 | `fontSizeHeading5`(16) |
| 卡片标题 h3 | 14 / 20 | 500 | 自定义 |
| **正文（基准）** | **13 / 20** | 400 | `fontSize`(13) |
| 元信息 | 12 / 16 | 400 | `fontSizeSM`(11~12) |
| 代码 / ID / 数字 | 12 / 16 mono | 400 | `fontFamilyCode` |
| 最小字号 | 11 | 400 | `fontSizeSM`(11) |

字重**只有 400 和 500 两档**，不用 600 / 700（AntD 的 `fontWeightStrong` 默认 600，已在 §3.6 改为 500）。

> **把基准字号压到 13px，是把"仪表盘"和"落地页"分开的第一刀。**
> AI 生成的界面默认 16px 基准、h1 36px 起步；AntD 默认 14px / h1 38px —— 都偏大。

### 4.3 等宽数字（强制）

任何会被纵向比较的数字，必须：

```css
.af-num {
  font-family: var(--af-font-mono);
  font-variant-numeric: tabular-nums;
}
```

适用范围：耗时（ms）、token 数、成本、得分、run id、trace id、span id、指纹（如 `a7797b06…90afe`）。
表格中的数字列**右对齐**。

> ⚠ **AntD 的 `Table` 不会自动给数字加 `tabular-nums`。** 要在列的 `render` 里套一层 `<span className="af-num">`，或用 `columns[].className` 配合全局样式。

---

## 5. 间距与密度

### 5.1 基准

**4px 基准网格。** 一切间距是 4 的倍数。（与 AntD 的 `sizeUnit: 4` 一致，不需要改。）

### 5.2 标准尺寸

| 对象 | 值 | AntD 默认 | 差 |
|---|---|---|---|
| 页面内边距 | 16 | — | — |
| 区块间距 | 16 / 24 / 32 三档 | — | — |
| 卡片内边距 | 12（紧凑）/ 16（默认） | Card body 默认 24 | **偏大，按需覆盖** |
| 控件高度 | 28（小）/ **32（默认）** / 36（主操作） | `controlHeight` 32 | ✅ 一致 |
| 列表行高 | 28（紧凑）/ **32（默认）** / 36（宽松） | Table 约 54 | **必须覆盖** |
| 表格单元格 padding | 上下 6 / 左右 12 | 上下 16 / 左右 16 | **必须覆盖** |

> **对照**：AI 生成的界面按钮高度默认 40，表格行高默认 48+。
> **AntD 在这两项上比 shadcn 好**：`controlHeight` 默认就是 32、`borderRadius` 默认就是 6 —— 也就是说"保留 AntD"这个决定，在密度这件事上的代价比预想的小。真正要动手的只有 **Table 的 padding** 和 **字号**。

### 5.3 密度守门线（验收标准，可量化）

| 页面 | 硬指标 |
|---|---|
| Trace 时间线 | 1440×900 视口内，首屏可见 **≥ 12 个 span** |
| 评测看板 | 首屏可见 **≥ 15 行**题目结果 |
| MCP 工具注册表 | 首屏可见 **≥ 10 个工具** |
| 对话运行台 | 单条消息（含工具调用卡片）折叠后高度 **≤ 72px** |

这几条不是"感觉"，是**验收时能数的**。达不到就是密度不够。

---

## 6. 形状与层次

### 6.1 圆角

`容器 6px` · `控件 4px` · `标签 4px` · **上限 6px**

禁止 `rounded-lg (8px)` 及以上、禁止胶囊按钮。
AntD 对应：`borderRadius: 6` / `borderRadiusSM: 4` / **`borderRadiusLG: 6`（默认 8，必须改）**。

### 6.2 层级表达顺序（严格按此顺序）

1. **背景微差** —— `canvas` 与 `surface` 的差别（`#F5F6F8` vs `#FFFFFF`）
2. **1px 描边** —— `border-default`
3. **阴影** —— 只在浮层用（dropdown / modal / tooltip），且必须是 §3.6 里那个单层阴影

**卡片一律不加阴影，加描边。** 这是 R02。
> ⚠ AntD 的 `Card` 默认就是"白底 + 1px 描边、无阴影"，**这一点不用改**。真正要管的是别自己往上加 `boxShadow`。

### 6.3 焦点环

```css
:focus-visible {
  outline: 2px solid var(--af-accent);
  outline-offset: 1px;
}
```

键盘焦点必须可见——这是无障碍要求，也是"这是个真工具"的信号。
**禁止 `outline: none` 而不提供替代。**

---

## 7. 状态与反馈

这一节是 AI 生成界面最偷懒的地方，也是最能拉开差距的地方。

| 状态 | 要求 | AntD 对应 |
|---|---|---|
| **加载** | 骨架屏，形状必须与真实内容一致（表格骨架就画 5 行格子，不是居中转圈） | `<Skeleton active />` 或 `Table loading={{ spinning, indicator }}` + 自定义骨架 |
| **空** | 一句人话 + 一个具体动作。例：**「还没有评测记录。跑一次评测 →」**（箭头是可点按钮） | `<Empty>` 的 `description` 要重写，**不要用默认插画**（默认插图是 AI 感的来源之一） |
| **错误** | 必须给可操作的下一步，不能只贴报错原文。例：**「Reranker 模型未找到。检查 `models/bge-reranker-base` 是否存在 →」** | `<Result status="error">` 或 `<Alert type="error">`，`action` 必须给 |
| **流式输出** | 光标用 2px 竖条。禁止打字机音效、禁止逐字缩放动画 | 自绘 |
| **禁用** | 用 `text-disabled`；降不透明度**只作用于背景**，文字保持可读 | AntD 默认已符合 |
| **长时运行** | 超过 2 秒必须有进度或耗时显示（"已运行 3.4s"），不能只有一个转圈 | `<Spin>` 只给 `<Button loading>` 用，页面级用进度条 + 计时文本 |

**动效预算**：只允许 150–200ms 的 `opacity` / `transform` 过渡。**禁止**入场动画、禁止列表 stagger、禁止任何循环播放的装饰动画（唯一例外：运行中的进度条）。
> AntD 的 `motionDurationMid` 默认 `0.2s` = 200ms，**在预算内，不用改**。`motionDurationSlow` 是 `0.3s`，被 Modal / Drawer 这类面板动画使用，可接受。

---

## 8. 五个核心页面的具体规则

### 8.1 Trace 时间线 —— **招牌页，最先做**

> 这一页 AntD 里**没有能直接用的组件**（`Timeline` 是竖向的，形状完全不对），要自绘。自绘部分的色值走 §3.5 的 CSS 变量。

- 横向 span 图，按时间比例缩放（不是等宽）
- 嵌套 span 用缩进 + 左侧竖线表示父子，不用括号
- 每行：`名称 | 耗时（等宽右对齐）| 状态色点`
- hover 出浮层（`surface-raised` + 描边 + §3.6 的单层阴影），显示起止时间、输入输出、token 数
- 时间轴刻度用 `text-tertiary`，密集但不抢戏
- **失败 span 不铺红底**，只在状态点上用 `danger`，并在行首加一个 2px 的红色左边条

### 8.2 运行台（对话 + 流式）

- 消息块之间用分隔线，不用气泡尾巴
- 工具调用渲染成可折叠卡片（`<Collapse ghost>` 或自绘），折叠态一行显示：`工具名 · 耗时 · 是否成功`
- 耗时 / token / 成本**内联在消息尾部**（12px mono，`text-tertiary`），不做成独立区块
- 用户消息与 Agent 消息靠**左对齐 + 背景微差**区分，不靠颜色

### 8.3 评测看板

这是你评测体系（50 题 / 三配置 / 两指纹）的门面。**最关键的一条：口径必须写在 UI 上，不能只留在文档里。**

- 用 `<Table size="middle">`（配合 §3.6 的 `cellPaddingBlockMD: 4`），三配置对比表
- 每配置**最新一轮**（对应取数规则 `distinct on (config_name) ... order by id desc`）
- **工具题要显式标注"只看工具调用，不计分"** —— 否则用户会疑惑为什么它是 `NULL`
- **均分口径要写在表头**：按题平均 vs 按行平均是**两个数**，且**不可互证**
- 分母显示 **40**（工具题 10 条不送 judge），不是 50 —— 这一条最容易被人误读，UI 上有责任说清
- 逐题下钻：并排比 `(判定, 分数)`，并**显示答案指纹**（如 `32d4d63c`）—— 因为三配置答案逐字不同的失败模式根本不能比，界面要能让人一眼看出"这三个东西不是同一个"
- 作废轮（run 1/4/5/6）不出现在界面里，或明确标灰

### 8.4 RAG 检索检查器

- 显示 top-k 片段（默认 k=5），每条带来源文档名 + 名次
- **重排前后名次变化要并排显示**（候选池 20 → 最终 5，这是一次真正的"取舍"）
- **分数必须标注是 logit，不是概率** —— 这是最容易被误读的一个数
- 命中判定用色点（`success`）= 是否命中 gold，不用彩色整行

### 8.5 MCP 工具注册表

- 工具清单列表，显示来源 Server 名 + 工具名（已定命名空间前缀）
- 点击展开渲染 `inputSchema`（从函数签名自动生成的 JSON Schema），用 `fontFamilyCode`
- 调用记录：时间、耗时、`isError`、错误层级（工具层 `isError=true` vs 协议层 JSON-RPC `error`）—— 这个区分是你项目里的硬知识点，UI 上体现出来
- 空态要写清"未连接 Server"，不要显示一个空表

---

## 9. 组件与工程约定

- **栈**：**Vite + React + TS + AntD（遵循 PRD 选型）+ `@ant-design/icons`**
  - 不引入 Tailwind。需要局部自定义样式时用 **CSS Modules**，不要用内联 `style` 堆样式
- **主题**：`<html data-theme="light|dark">`，默认 light，选择持久化到 `localStorage`
  - 主题状态同时驱动 **AntD 的 `<ConfigProvider theme>`** 和 **`data-theme` 属性**，两者必须同源同步
- **主题配置唯一入口**：`src/theme/antdTheme.ts`（§3.6）。**禁止在组件里写 `<ConfigProvider>` 局部覆盖** —— 一处例外：某个组件确实需要独有样式时用 `components` 字段在那个唯一入口里加，不要散落各处
- **色板唯一真源**：`src/theme/palette.ts`（§3.5 末尾）。AntD token 与 CSS 变量都从它读
- **禁止裸色值**：组件里不许出现 `#fff`、`color: 'red'`、`background: '#f5f5f5'`。需要颜色时读 §3.5 的 CSS 变量，或从 `theme.useToken()` 拿。这一条建议用 stylelint 规则或 CI grep 卡住
- **图表明暗双主题**：图表库（ECharts 等）不吃 AntD 的 token，**必须把浅/深两套色值显式传进图表配置**，切换主题时重建实例。这是实践中最容易漏的一处
- **AntD 覆盖不到的边界（诚实交代，别指望 token 万能）**：
  1. AntD 组件**内部结构**的间距 —— 部分有组件 token，部分只能靠 CSS Modules 覆盖（比如 `Form.Item` 的默认 `margin-bottom: 24px`）
  2. `Table` 在 `virtual`（虚拟滚动）模式下，行高由 `scroll.y` 参与计算，**token 的影响有限**，要单独调
  3. 部分组件（`Descriptions`、`Statistic`、`Empty` 的默认插画）视觉气质偏"中后台"，token 改不动，**要么少用、要么整个替换掉**
  4. → **落地时的自查方法**：改完 token 先看一遍所有用到的组件，把"改了 token 还是不对"的列出来，逐个决定是加 `components` 配置还是 CSS 覆盖

---

## 10. 落地顺序

不改现有里程碑编号，只标前端内部顺序：

1. **主题骨架先落地** —— `palette.ts` + `antdTheme.ts` + `<ConfigProvider>` + `data-theme` 切换。**没这一步，后面每一页都在裸写色值**
2. **Trace 时间线单页** —— 最难的一页先做，用真实数据把调性钉死。**这一页做完就能拿去面试**
3. 运行台（流式）
4. 评测看板
5. 检索检查器 + 工具注册表

**动工前置条件**：

- [ ] `docs/frontend/refs/` 里**至少有 1 张同类产品的截图** —— 第一页是 Trace 页，所以有 1 张 Langfuse / Datadog 的 trace 截图即可开工；其余 4 张**边做边补**（★ 原「≥5 张才动工」的说法已修正，见 §1.2）
- [x] §3.4 的强调色已拍板 → **青 `#0E6F63` / `#4FD1B8`**
- [x] §9 的组件库已定 → **AntD（遵循 PRD）**
- [x] §3.6 的 AntD 主题映射已产出

---

## 附录 A · 未决与待办

| 项 | 说明 | 状态 |
|---|---|---|
| 强调色 | §3.4 | ✅ 已定（青） |
| 组件库 | §9 | ✅ 已定（AntD，遵循 PRD） |
| AntD 主题映射 | §3.6 | ✅ 已产出（Layout / Table 已核对源码；其余组件按需扩写） |
| **参考图收集** | §1.2，存 `docs/frontend/refs/`。★ **开工门槛已修正为「≥1 张同类截图」，不是「≥5 张」**；其余边做边补 | ⏳ 待收集（**不构成硬门槛**） |
| AntD `cssVar` 变量命名 | §3.5 末尾，变量名格式未实跑验证 | ⏳ 待实跑 |
| 图表双主题方案 | §9，选库后确认 | ⏳ 未开始 |
| 本文档与 PRD 的关系 | 本文档是**前端唯一视觉权威**；PRD 管功能范围，冲突时功能听 PRD、视觉听本文档 | ✅ 已定 |
| v1.0 → v1.1 的技术选型变更 | **已解决**：v1.0 曾按 Tailwind + shadcn/ui 写，现改回 PRD 的 AntD，**PRD 不需要任何改动** | ✅ 已解决 |
