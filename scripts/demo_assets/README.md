# demo_assets —— 演示素材的**源文件**（不是素材本身）

`data/sample_docs/` 里的三份示例文档是**素材**（会被上传进知识库）；
本目录放的是其中 **PDF 那份的 HTML 源**，用途只有一个：**让 PDF 可重新生成**。

```
scripts/demo_assets/产品手册.html   ← 源
        ↓  Chrome headless 打印
data/sample_docs/产品手册.pdf        ← 素材（已提交进仓）
```

另外两份（`公司考勤制度.md`、`FAQ-常见问题.md`）是纯文本，**源即素材**，不需要本目录。

---

## 重新生成 PDF

```bash
# 1) 把 HTML 路径转成 file:// URI（中文文件名必须转义）
URI=$(python3 -c "from pathlib import Path; print(Path('scripts/demo_assets/产品手册.html').resolve().as_uri())")

# 2) 用 Chrome headless 打印成 PDF
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="data/sample_docs/产品手册.pdf" "$URI"

# 3) ★ 必须校验（见下）
python - <<'PY'
from pypdf import PdfReader
r = PdfReader('data/sample_docs/产品手册.pdf')
full = ''.join((p.extract_text() or '') for p in r.pages)
bad = [f"{c} U+{ord(c):04X}" for c in full if 0x2E80 <= ord(c) <= 0x2FDF]
print('页数:', len(r.pages))
print('部首区字符（必须为 0）:', len(bad), bad[:10])
PY
```

---

## ★ 字体陷阱：为什么 CSS 里写死了 `Arial Unicode MS`

**现象**：用 macOS 的常见中文字体（`PingFang SC` / `Songti SC` / `Heiti SC` / `Hiragino Sans GB`）生成 PDF 时，
Chrome 写进 PDF 的 **ToUnicode 映射表会把若干净字指向「Kangxi 部首」区的码位**：

| 本应 | 实际落进 PDF | 后果 |
|---|---|---|
| `手` U+624B | `⼿` U+2F3F（KANGXI RADICAL HAND） | 肉眼几乎无差别 |
| `文` U+6587 | `⽂` U+2F42 | 同上 |
| `门` U+95E8 | `⻔` U+2ED4（CJK RADICAL C-SIMPLIFIED GATE） | 同上 |
| `一` U+4E00 | `⼀` U+2F00 | 同上 |

**为什么这是「静默」的、且必须当场修掉**：

1. **肉眼看不出来** —— 部首区字形与正字字形几乎一致，人和阅读器都察觉不到；
2. **关键词检索直接失配** —— 入库文本是 `⼿册` 而用户搜 `手册`，PostgreSQL 全文索引**匹配不到**，
   表现为「这篇文档明明有，却搜不出来」，且不报任何错；
3. **NFKC 也修不干净** —— `⼿`→`手` 能还原，但 `⻔` 是 CJK 部首补充区（U+2ED4），
   **NFKC 归一化后仍是 `⻔`**。所以「入库时统一做一次 NFKC」这条退路**不成立**，只能从生成端解决。

**实测结果**（同一份 HTML，只换字体，`0x2E80–0x2FDF` 区字符数）：

| 字体 | 部首区字符数 |
|---|---|
| PingFang SC | 9 |
| Heiti SC | 9 |
| Songti SC / STSong / Hiragino Sans GB | 8 |
| **Arial Unicode MS** | **0** ✅ |

**结论**：生成端字体固定为 `Arial Unicode MS`（macOS 自带，位于 `/System/Library/Fonts/Supplemental/`），
并且**每次重新生成后都必须跑一遍上面的校验** —— 一旦字体被换掉或该字体缺失导致浏览器回退，
问题会**静默复现**，而 PDF 提交进仓后不会有人再看一眼。

> 这条是「**看起来对了 ≠ 真的对了**」的又一实例：PDF 打开、文字清晰、页码正确，
> 但底下的码位是错的。**判据不能是「我看了觉得没问题」，只能是对提取文本做字符区间的机检。**
