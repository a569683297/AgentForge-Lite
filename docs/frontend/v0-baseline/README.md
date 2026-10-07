# v0-baseline/ —— v0 生成的前端视觉基线（留档，不是生产代码）

> ⚠ **这两个文件不是本项目的代码，不要 import、不要改。** 它们是从 `v0`（AI 生成 UI 的工具）
> 生成的**一次性视觉稿**，2026-10-07 从 `~/Downloads/agent-forge-lite/` 复制进来留档。

## 为什么留它

`docs/frontend/IMPL-SPEC.md` 与 `DESIGN.md` 的**视觉基线就是它** ——
色板取值、间距密度、卡片/表格长相都以这份稿子为准在 `DESIGN.md` 里被"翻译"成了规范。
把它复制进仓库是为了让 **`docs/frontend/` 这套交付自包含**：
只看 `docs/frontend/` 就能复核「规范说的那个颜色，到底长什么样」。

## 文件

| 文件 | 原路径 | 是什么 |
|---|---|---|
| `page.tsx` | `~/Downloads/agent-forge-lite/app/page.tsx`（129 行 / 19 KB） | 那份视觉稿的**单页实现**（Next.js + Tailwind 写的） |
| `dashboard.css` | `~/Downloads/agent-forge-lite/app/dashboard.css`（17 KB，已压缩） | 它带来的**样式表**，★ **色板变量 `:root` 就在这个文件的第 3 行** |

## ★ 复核色板时的正确读法（这是留档的主要用途）

`dashboard.css` 第 3 行的 `:root` 是**唯一真源的出处**，本项目的 `frontend/src/theme/palette.ts` 就是照它抄的。
能对上的（浅色）：

```
--canvas:#f5f6f8   --surface:#fff   --sunken:#eceef1   --border:#e4e7eb
--text:#14181e     --secondary:#4a5361   --tertiary:#667080
--accent:#0e6f63   ← 强调色（青）  --accent-soft:#e2f2ef   ← ★ DESIGN §3.5 当初漏了这个，本次补进 palette
--success:#0b7a46  --danger:#c0271d  --warning:#8a5a08  --info:#0b6ba8
```

## ⚠ 有两处**故意没跟**它（别以为抄漏了）

1. **字体**：v0 用的是 `--sans:Inter,...` 与 `--mono:'DM Mono',...`
   → 本项目**故意删掉 Inter 与 DM Mono**，改走系统字体栈（理由见 `DESIGN.md` §4.1：
   「不要引入 Inter」—— 它是 v0 / shadcn 的事实默认字体，用它等于自动加入平均值）。
2. **组件库**：v0 走 Tailwind + shadcn 风格；本项目**走 AntD**（PRD 定死）。
   所以这份稿子只能当**视觉标尺**用，**不能当代码来源**。

## 与 `../refs/` 的区别（别搞混）

- `../refs/` 存的是**真人成熟产品的截图**（Langfuse / Linear / Datadog…），用来当"密度与调性的外部标尺"。
- 本目录存的是**我们自己的 v0 稿**，用来当"**本项目视觉规范的历史出处**"。
- 按 `../refs/README.md` 的纪律，v0 属于**概念稿**，**不该**放进 `refs/` —— 所以单开本目录。
