"""
D39 前置①：把 AgentForge 的 11 篇语料**同源**灌入 Dify 知识库
================================================================

## 为什么需要这个脚本

PRD §9.11.1 前提 1「**文档同源**」要求"同一批文档**同时入库两侧**"。现状：

    AgentForge 侧：11 篇 / 82 片（8 篇 d21 语料 + 3 份 sample_docs）
    Dify 侧      ：1 份 content_dify.txt / 30 块 ← 这是 9-21 的实验品，与评测集无关

不补齐 Dify 这一侧，D39 的同题对照结论**直接作废**（前提 1 不满足的后果，
PRD 原文写的就是"对照结论直接作废——两边喂的文档不同，量纲都不一样"）。

## 关键设计（每条都有依据，不是随手定的）

### 1. 11 篇全部走 `create-by-text`，**不用** `create-by-file`

- 8 篇合成语料的源在 `scripts/d21_corpus.py` 的 `DOCUMENTS` **Python 字面量**里，
  磁盘上根本没有对应文件 —— 用 text 接口正好免去"先落文件"这一步；
- 另 2 篇（`FAQ-常见问题.md` / `公司考勤制度.md`）是**真 Markdown**：若走 `create-by-file`，
  Dify 的 `ExtractProcessor` 会按后缀选 `MarkdownExtractor`，它**先用 `^#+\\s` 把文档
  按标题预切成多个独立文档**，之后你设的 `separator` 套在每个"标题段"上、永不触发
  —— 这正是本项目 skill 记录的**头号陷阱**（症状：改分隔符，块数一个不变）。
- `产品手册.pdf` 必须先解析成文本（见下），同样走 text。

> ⚠ **〔2026-10-10 更正〕这里原先还写着一句「`create-by-text` 的 `data_source_type='text'`
> 不经过抽取器，陷阱自动消失」—— 这句是错的，已删。**
>
> 实测：`create-by-text` 建出来的文档 `data_source_type` 是 **`upload_file`**，不是 `text`。
> 看源码就明白 —— `controllers/service_api/dataset/document.py::_create_document_by_text`
> 先调 `FileService(db.engine).upload_text(text=…, text_name=…)` **把文本落成文件**，
> 再走标准上传流程。所以它是"帮你落文件"，**不是"绕过抽取器"**。
> 走 text 接口的真实收益只有一个：**不必自己造文件**。
>
> 至于**头号陷阱为何没触发**（11 篇块数与"整篇落进递归切分"的预期一致）——
> **归因未锁定，如实登记**。已排除"其实走了 `MarkdownExtractor`"这个解释：
> 直接调它会剥掉**全部** `#` 并把一篇切成 7 份，与实测"只丢 1 个 `#`、切成 11 块"不符。
> 未查明的原因不编。

### 2. 分块参数（2026-10-10 实测确定，不是抄来的）

    max_tokens = 300     chunk_overlap = 50     separator = "@@@__NOSPLIT__@@@"

- **★ `max_tokens` 在 Dify 里实际是「字符数」，不是 token 数。**
  依据：`api/core/rag/splitter/fixed_text_splitter.py` 里
  `EnhancedRecursiveCharacterTextSplitter.from_encoder()` 把 token 计数器**丢掉了**——
  `_ = _token_encoder  # kept for future token-length wiring`，返回时用的是
  `_character_encoder`（就是 `len(text)`）。
  → 所以它和 AgentForge 的 `CHUNK_SIZE = 300 字符` 是**同一个量纲**，可以直接同值。
- **`separator` 必须取「文档中绝不出现」的串（本脚本用 `@@@__NOSPLIT__@@@`）。**
  原因：只有让外层的 `split` 切不动、整篇落进递归路径，**合并**才会发生（见下表）。
  早先误用**换行符**当分隔符，11 篇实测得到 **322 块**（本地 82 片，3.93x），
  FAQ 里出现了只有 **7 个字符**的碎片 —— 这条弯路的完整数据见下表。
- ⚠ **仍存在、且已接受的不对齐（须写进对照报告免责声明）**：
  即便块数量级对齐（见下表），**断点位置仍不同** —— AgentForge 是纯字符硬切
  （每 300 字符一刀，会从句子中间切断），Dify 只在标点/换行处断。
  这是两套切分机制的固有差异，不是"配置没调对"，无法消除。
  PRD 前提 1 管的是"文件集合"（哪些文档），没管"切法"（怎么切）；
  切法差异本身正属"自建可精确控 vs 平台黑盒"的对照点，如实登记即可。

**全量实测（11 篇，`/tmp/probe_split_all.py`；本地基线 82 片）**：

| separator | max_tokens | 合计块数 | 比值 | 单篇最差 |
|---|---|---|---|---|
| `"\n"` | 300 | 322 | 3.93x | 8.38x（产品手册 109 vs 13） |
| **占位串** | **300** | **88** | **1.07x** | **1.22x** ★ 本脚本采用 |
| 占位串 | 400 | 63 | 0.77x | — |
| 占位串 | 500 | 50 | 0.61x | — |

### 3. 预处理规则**全部关闭**

AgentForge 入库**不做任何文本预处理**。Dify 若开 `remove_extra_spaces`，其实现是
`re.sub(r"\\n{3,}", "\\n\\n", text)`（比界面文案说的"替换连续空格换行制表符"弱得多）——
对本批语料虽无实际影响（单换行分隔，没有 3 连换行），但**为满足"同源"仍一律关掉**：
两侧的输入文本必须逐字一致，不能靠"反正这次不影响"来赌。

### 4. 文本来源必须与 AgentForge 入库时**逐字一致**

- 8 篇合成语料 → `d21_corpus.full_text(lines)`，**就是**入库走的那份原文
  （`ingest_texts → _split_segments → split_text` 的输入）；
- 3 份 sample_docs → 调 **AgentForge 自己的** `document_parser.parse_file()` 再拼接各段。
  **不在这里重新实现一遍解析** —— 两份解析逻辑一旦漂移，同源就名存实亡。

## 用法

    export DIFY_DATASET_API_KEY=dataset-xxxxx
    cd <项目根>
    ~/.local/bin/uv run python -m scripts.d39_dify_ingest                 # 建库 + 灌 11 篇
    ~/.local/bin/uv run python -m scripts.d39_dify_ingest --dry-run       # 只打印要做什么
    ~/.local/bin/uv run python -m scripts.d39_dify_ingest --verify-only   # 只核对已入库结果

**API key 从环境变量读，不落仓库**（dataset key 只授知识库读写，可随时在 Dify 撤销）。

## 幂等性

- 知识库：按**名字**判断，已存在则复用（不重复建）；
- 文档：按**文件名**判断，已存在则跳过 —— 可安全重跑。

## 退出码（与 `seed_demo_data.py` / `warmup_mcp.py` 同构）

| 码 | 含义 |
|---|---|
| **0** | 11 篇全部 `indexing_status=completed` |
| **1** | 有文档 `error` 或轮询超时（**服务是活的，但入库没成功**） |
| **2** | 前置不满足：无 API key / Dify 不可达 / 素材文件缺失（**不是坏了，是还没起**） |

## 跑完之后还需要手动做一步

新建的库**还没有挂到任何应用上** —— 光有库、没有应用引用它，同题对照就跑不起来。

- 现成的应用「陪跑」**不适合当对照对象**，不能直接换库了事。它的 `pre_prompt` 是
  **前端面试官人格 + 强制输出格式**（✅对/❌缺/🔍追问/📊评分）。judge 收到的
  「待评答案」会是这种"面试官回应"，而不是对题目的回答 —— rubric 里"格式不计分"
  指的是措辞与详略，不是"答非所问也算分"，实际会被判 `correctness=0`。
  用它对照 = 拿人格差异冒充 RAG 能力差异，结论作废。
- 正确做法：**新建一个中性问答应用**挂本库，并用它的 **Service API key（`app-` 前缀）**
  走 `POST /v1/chat-messages`。这比早先记录的
  `X-App-Code` → `/api/passport` → `/api/chat-messages`（web 内部接口）稳，
  且 `app-` key 可以独立轮换、独立撤销。
- ⚠ dataset key **不授应用配置权限**（`api_tokens` 里 `type='dataset'` 没有 `app_id`），
  所以"建应用 / 挂库 / 拿 app key"这三步只能在 Dify 界面做。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from app.services.document_parser import parse_file
from scripts.d21_corpus import DOCUMENTS, SLUG_TO_FILENAME, full_text

# ---------------------------------------------------------------- 常量

SAMPLE_DIR = Path("data/sample_docs")

# PRD §17.2 的三份演示文档；顺序与 seed_demo_data.py 保持一致
SAMPLE_FILES: tuple[str, ...] = ("公司考勤制度.md", "FAQ-常见问题.md", "产品手册.pdf")

# 新建的知识库名（Dify 限制 ≤ 40 字符）
DATASET_NAME = "agentforge-corpus-11"
DATASET_DESC = "D39 对照基线：与 AgentForge documents 表同一批文档（11 篇）"

DEFAULT_BASE_URL = "http://localhost/v1"

# 分块参数 —— 见模块 docstring §2 的实测依据
CHUNK_SIZE = 300
CHUNK_OVERLAP = 50

# ★ separator 必须取一个**文档中绝不会出现**的字符串，**不能**用 "\n"。
# 根因：Dify 的 FixedRecursiveCharacterTextSplitter 对外层按 separator 切出来的段
#       **不做任何合并** —— 只有"长度 > chunk_size"的段才会进 recursive_split_text，
#       而合并逻辑（_merge_splits）只存在于那条路径里。
#   · separator="\n"：Markdown/PDF 每行都 < 300 → 全部走"不大就原样收"分支 → 逐行成块，永不合并
#   · 本配置（文档里没有这个串）：外层切不动 → 整篇为一个段 → 因 > 300 进递归 → 才发生合并
# 实测（11 篇全量，2026-10-10）：
#   separator="\n"  → 322 块（本地 82 片，3.93x；单篇最差 8.38x）
#   本配置          →  88 块（1.07x；单篇 0.92x~1.22x）★ 选取
SEPARATOR = "@@@__NOSPLIT__@@@"

POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0
HTTP_TIMEOUT_S = 120.0

TERMINAL_OK = "completed"
TERMINAL_BAD = "error"


# ---------------------------------------------------------------- 输出

def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def row(*cells: object, widths: tuple[int, ...] | None = None) -> None:
    if widths is None:
        print("  ".join(str(c) for c in cells))
        return
    print("  ".join(str(c).ljust(w) for c, w in zip(cells, widths)))


# ---------------------------------------------------------------- 文本收集

async def collect_documents() -> list[tuple[str, str]]:
    """
    收集 11 篇文档的 (文件名, 全文)。

    文本一律取自 AgentForge 侧的**同一份来源**，保证与库中内容逐字同源：
      · 8 篇合成语料 → d21_corpus.full_text()，正是 ingest_texts 的输入
      · 3 份示例素材 → AgentForge 自己的 document_parser.parse_file()
    """
    docs: list[tuple[str, str]] = []

    # ① 8 篇合成语料（源是 Python 字面量）
    for _slug, _filename, lines in DOCUMENTS:
        filename = SLUG_TO_FILENAME[_slug]
        docs.append((filename, full_text(lines)))

    # ② 3 份示例素材（源是磁盘文件，但必须用 AgentForge 的解析器）
    for filename in SAMPLE_FILES:
        path = SAMPLE_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"素材缺失：{path}")
        segments = await parse_file(filename, path.read_bytes())
        if not segments:
            raise RuntimeError(f"解析为空：{filename}")
        docs.append((filename, "\n".join(s.text for s in segments)))

    # 防御：占位分隔符若真的出现在正文里，切分就完全错了 —— 宁可拦下也不静默出错。
    # （这批语料里不可能有它，此断言是防止将来换语料时踩到。）
    for name, text in docs:
        if SEPARATOR in text:
            raise RuntimeError(
                f"{name} 的正文里出现了占位分隔符 {SEPARATOR!r} —— 请换一个文档中不存在的串"
            )

    return docs


# ---------------------------------------------------------------- Dify API

class DifyClient:
    """
    Dify 知识库 Service API 的最小客户端。

    只包这个脚本要用的三个端点（其余一律不碰）：
        POST /datasets                                     建库
        POST /datasets/<id>/document/create-by-text        建文档（带 process_rule）
        GET  /datasets/<id>/documents                      列文档（用于幂等与验收）
    """

    def __init__(self, base_url: str, api_key: str, *, dry_run: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dry_run = dry_run
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> DifyClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=HTTP_TIMEOUT_S,
            # 本机有代理在应答时，不加这个会拿到"看起来像服务挂了"的假状态码
            trust_env=False,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "必须在 async with 内使用"
        return self._client

    async def ping(self) -> None:
        """连通性 + key 有效性。失败直接抛，由上层转成退出码 2。"""
        resp = await self.client.get("/datasets", params={"page": 1, "limit": 1})
        resp.raise_for_status()

    async def list_datasets(self) -> list[dict[str, Any]]:
        resp = await self.client.get("/datasets", params={"page": 1, "limit": 100})
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def find_dataset(self) -> dict[str, Any] | None:
        """按名字找知识库。**找到就返回，找不到返回 None —— 绝不顺手建一个。**"""
        for ds in await self.list_datasets():
            if ds.get("name") == DATASET_NAME:
                return ds
        return None

    async def ensure_dataset(self) -> str:
        """按名字找知识库；没有则建。返回 dataset_id。"""
        if not self.dry_run:
            found = await self.find_dataset()
            if found is not None:
                print(f"  已存在同名知识库，复用：{found['id']}")
                return str(found["id"])

        payload = {
            "name": DATASET_NAME,
            "description": DATASET_DESC,
            "indexing_technique": "high_quality",
            # 与旧库一致：bge-m3（1024 维）。注意它和 AgentForge 的 vector(512)
            # 并非同源模型 —— 这一条属"已登记的不同源项"，见模块 docstring。
            "embedding_model": "bge-m3",
            "embedding_model_provider": "langgenius/ollama/ollama",
            "permission": "only_me",
        }
        if self.dry_run:
            print(f"  [dry-run] 将新建知识库：{DATASET_NAME}")
            return "DRY-RUN-DATASET-ID"

        resp = await self.client.post("/datasets", json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"建库失败 HTTP {resp.status_code}: {resp.text[:400]}")
        ds = resp.json()
        print(f"  已新建知识库：{ds['id']}（{ds['name']}）")
        return str(ds["id"])

    async def list_documents(self, dataset_id: str) -> list[dict[str, Any]]:
        resp = await self.client.get(f"/datasets/{dataset_id}/documents", params={"limit": 100})
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def create_by_text(self, dataset_id: str, name: str, text: str) -> dict[str, Any]:
        payload = {
            "name": name,
            "text": text,
            "indexing_technique": "high_quality",
            "doc_form": "text_model",
            "process_rule": {
                "mode": "custom",
                "rules": {
                    # ★ 全关：两侧输入必须逐字一致（见 docstring §3）
                    "pre_processing_rules": [
                        {"id": "remove_extra_spaces", "enabled": False},
                        {"id": "remove_urls_emails", "enabled": False},
                    ],
                    "segmentation": {
                        "separator": SEPARATOR,
                        "max_tokens": CHUNK_SIZE,
                        "chunk_overlap": CHUNK_OVERLAP,
                    },
                },
            },
        }
        resp = await self.client.post(
            f"/datasets/{dataset_id}/document/create-by-text", json=payload
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"建文档失败 HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    async def wait_terminal(self, dataset_id: str, doc_id: str) -> dict[str, Any]:
        """轮询到索引终态（completed / error）。"""
        waited = 0.0
        last: dict[str, Any] = {}
        while waited < POLL_TIMEOUT_S:
            resp = await self.client.get(f"/datasets/{dataset_id}/documents/{doc_id}")
            resp.raise_for_status()
            last = resp.json()
            status = last.get("indexing_status")
            if status in (TERMINAL_OK, TERMINAL_BAD):
                return last
            await asyncio.sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S
        last["_timeout"] = True
        return last


# ---------------------------------------------------------------- 验收

async def verify_documents(
    dify: DifyClient, dataset_id: str, doc_ids: dict[str, str]
) -> tuple[list[str], int]:
    """
    逐篇轮询到终态并打印验收表。返回（失败文件名清单, 分块合计）。

    与入库解耦，**`--verify-only` 复用同一份代码** —— 若两处各写一份验收逻辑，
    就会出现"灌完时按 A 判、复验时按 B 判"的错位，而且不会有任何报错。
    """
    # ⚠ 该端点**不返回 `word_count`**（DocumentDetailResponse 里没有这个字段），
    #   早先按 word_count 读会恒得 0 —— 那是检查器错，不是入库失败。
    #   真实可用的验收字段是 segment_count / average_segment_length。
    widths = (34, 12, 8, 8, 6)
    row("文件名", "indexing", "块数", "均长", "状态", widths=widths)
    row("-" * 34, "-" * 12, "-" * 8, "-" * 8, "-" * 6, widths=widths)

    failed: list[str] = []
    total_segments = 0
    for name, doc_id in doc_ids.items():
        info = await dify.wait_terminal(dataset_id, doc_id)
        status = info.get("indexing_status", "?")
        segs = info.get("segment_count") or 0
        avg = info.get("average_segment_length") or 0
        total_segments += segs
        # ★ 块数为 0 也判失败：Dify 有"0 分块却标 completed"的前科（界面绿勾不等于索引成功）
        bad = bool(info.get("_timeout")) or status == TERMINAL_BAD or segs == 0
        mark = "❌" if bad else "✅"
        if bad:
            failed.append(name)
        row(f"{name:<34}", f"{status:<12}", f"{segs:<8}", f"{avg:<8}", mark, widths=widths)
        if info.get("error"):
            print(f"      error: {str(info['error'])[:200]}")

    return failed, total_segments


def print_tail(dataset_id: str, skipped: list[str], failed: list[str]) -> None:
    if skipped:
        print(f"\n  跳过 {len(skipped)} 篇（已存在）")

    print(f"\n  知识库：{DATASET_NAME}")
    print(f"  dataset_id：{dataset_id}")
    print("\n  下一步验收（块数与向量对齐，界面绿勾不算数）：")
    print("    bash ~/dify/check-dify.sh vectors")
    print("    bash ~/dify/check-dify.sh dataset")
    print("\n  ⚠ 还需在 Dify 界面把**对照用的应用**的知识库关联切到本库：")
    print("      · dataset key 不授应用配置权限，这一步只能 UI 做；")
    print("      · 不要把现有「陪跑」当对照对象 —— 它的 pre_prompt 是前端面试官人格 +")
    print("        强制输出格式（✅对/❌缺/🔍追问/📊评分），judge 收到这种'回应'只会判 0 分；")
    print("      · 建议新建一个中性问答应用挂本库，并用它的 Service API key（app-xxx）调用")


# ---------------------------------------------------------------- 主流程

async def run_verify_only(args: argparse.Namespace, api_key: str) -> int:
    """
    只核对已入库结果，**不写任何数据**。

    为什么要有这条路径：D39 期间会反复回来看"库还在不在、块还是不是 88、
    有没有哪篇悄悄变 error"。跑一次完整入库来"顺便验收"代价太大，
    而且它会在库里留下痕迹 —— 验收动作不该有副作用。
    """
    section("仅核对：不写入任何数据")
    async with DifyClient(args.base_url, api_key) as dify:
        try:
            await dify.ping()
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Dify 不可达或 key 无效：{type(exc).__name__}: {exc}")
            return 2

        found = await dify.find_dataset()
        if found is None:
            print(f"  ❌ 找不到知识库 {DATASET_NAME!r} —— 先跑一次完整入库")
            return 2
        dataset_id = str(found["id"])
        print(f"  知识库：{DATASET_NAME}（{dataset_id}）")

        docs = await dify.list_documents(dataset_id)
        if not docs:
            print("  ❌ 库里一篇文档都没有")
            return 1
        doc_ids = {d.get("name", ""): d.get("id", "") for d in docs}

        # 库里有、清单里没有 = 残留（换语料时最容易被漏掉）。必须说出来，不能只报"11 篇都对"。
        expected = {SLUG_TO_FILENAME[slug] for slug, _f, _lines in DOCUMENTS} | set(SAMPLE_FILES)
        extra = sorted(set(doc_ids) - expected)
        missing = sorted(expected - set(doc_ids))
        if extra:
            print(f"  ⚠ 库里多出 {len(extra)} 篇**不在本次清单**的残留：{', '.join(extra)}")
        if missing:
            print(f"  ⚠ 清单里有 {len(missing)} 篇**没入库**：{', '.join(missing)}")

        failed, total_segments = await verify_documents(dify, dataset_id, doc_ids)
        print(f"\n  分块合计：{total_segments} 块")
        print_tail(dataset_id, [], failed)

        if failed or extra or missing:
            return 1
    return 0


async def run(args: argparse.Namespace) -> int:
    # key 检查提到最前面：`--verify-only` 也要发真实请求，两条路径的判据必须同一处写
    api_key = args.api_key or os.environ.get("DIFY_DATASET_API_KEY", "")
    if not api_key and not args.dry_run:
        print("\n  ❌ 缺少 API key：请设 DIFY_DATASET_API_KEY 或传 --api-key")
        return 2

    if args.verify_only:
        return await run_verify_only(args, api_key)

    section("1/3 收集 11 篇文档的原文（必须与 AgentForge 入库时逐字一致）")
    try:
        docs = await collect_documents()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"  ❌ 素材不满足：{exc}")
        return 2

    total_chars = sum(len(t) for _, t in docs)
    for name, text in docs:
        row(f"{name:<34}", f"{len(text):>6} 字符")
    print(f"\n  合计 {len(docs)} 篇 / {total_chars} 字符")
    if len(docs) != 11:
        print(f"  ⚠ 预期 11 篇，实际 {len(docs)} 篇 —— 请核对来源清单")

    section("2/3 建库 + 逐篇入库")
    async with DifyClient(args.base_url, api_key or "dry-run", dry_run=args.dry_run) as dify:
        if not args.dry_run:
            try:
                await dify.ping()
                print(f"  Dify 可达，key 有效：{args.base_url}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ Dify 不可达或 key 无效：{type(exc).__name__}: {exc}")
                return 2

        dataset_id = await dify.ensure_dataset()

        existing: dict[str, str] = {}
        if not args.dry_run:
            for item in await dify.list_documents(dataset_id):
                existing[item.get("name", "")] = item.get("id", "")

        uploaded: dict[str, str] = {}
        skipped: list[str] = []

        for name, text in docs:
            if name in existing:
                skipped.append(name)
                row(f"  SKIP {name:<30}", "（已入库，按名跳过）")
                uploaded[name] = existing[name]
                continue

            if args.dry_run:
                row(f"  [dry-run] {name:<28}", f"{len(text):>6} 字符 → create-by-text")
                continue

            result = await dify.create_by_text(dataset_id, name, text)
            doc_id = result.get("document", {}).get("id", "")
            uploaded[name] = doc_id
            row(f"  提交 {name:<30}", f"doc_id={doc_id[:8]}…")

        if args.dry_run:
            print("\n  [dry-run] 未真正写入。去掉 --dry-run 即执行。")
            return 0

        section("3/3 等待索引完成并验收")
        failed, total_segments = await verify_documents(dify, dataset_id, uploaded)
        print(f"\n  分块合计：{total_segments} 块")
        print_tail(dataset_id, skipped, failed)

        if failed:
            print(f"\n  ❌ {len(failed)} 篇未成功：{', '.join(failed)}")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把 11 篇语料同源灌入 Dify 知识库（D39 前置①）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Dify Service API 根，默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--api-key", default="", help="dataset API key；不给则读 DIFY_DATASET_API_KEY")
    parser.add_argument("--dry-run", action="store_true", help="只打印将做什么，不写入")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="不写入，只按名字找到已有知识库并核对每篇的索引状态与块数",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
