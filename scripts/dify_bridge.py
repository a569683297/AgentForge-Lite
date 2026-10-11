"""D39 前置③：Dify 侧作答桥接 —— 把评测题发给 Dify 应用，取回答案 + chunk 级检索结果。

为什么需要它
------------
PRD §9.11.2 的执行流程是「① 前置 → ② 30 条同题两侧作答 → ③ 同一 judge → ④ 报告」。
本脚本负责第 ② 步里 **Dify 那一侧**：把 A 组 30 条 `doc_qa` 题逐条发给 Dify 应用
`agentforge-qa` 的 Service API，把 `answer` 与 `retriever_resources` 落成 JSON，
供 `scripts/eval_compare.py` 与 AgentForge 侧结果并排送进**同一个 judge**。

产出格式为什么长这样（★ 别改，改了「同一 judge」这句话就不成立）
------------------------------------------------------------------
AgentForge 侧的 chunks 由 `app/services/eval_runner.py::_format_chunks` 生成：

    "\\n\\n".join(f"[{index}] 来源：{source}\\n{content}" for item in result.sources)

Dify 侧 `metadata.retriever_resources[]` 与 `SourceItem` 字段**同构**（2026-10-10 实测）：

    document_name  ↔ SourceItem.source       （文件名）
    content        ↔ SourceItem.content      （片段正文）
    score          ↔ SourceItem.similarity   （余弦/融合得分）
    page           ↔ SourceItem.page_ref     （页码）

所以本脚本按**同一模板**拼 chunks —— 两侧喂进 judge 的东西形状一致，
差异只来自「检索到了什么」，不来自「怎么拼的」。

必须剥 <think>
--------------
实测（2026-10-10，问「公司年假有多少天？」）：`answer` 长度 **5277 字符**，
其中绝大部分是 `<think>…</think>` 思考块（deepseek-flash 输出，内含
`<!--dify-deepseek-reasoning-->` 标记）。不剥的话 judge 的【待评答案】收到的
是一堆内心独白 → **直接污染正确性打分**。

单轮、不带会话
--------------
每条题都不传 `conversation_id`，`user` 固定为 `d39-eval` → 单轮独立作答。
理由同 `app/services/eval_runner.py`：评测不是用户对话，题与题之间不能互相污染。

用法
----
    uv run python -m scripts.dify_bridge --dry-run      # 只列题，不发请求
    uv run python -m scripts.dify_bridge --limit 5      # 试跑 5 条
    uv run python -m scripts.dify_bridge                # A 组 30 条全量（约 5 分钟）

凭据从 `.env` 读（`DIFY_APP_API_KEY` / `DIFY_BASE_URL`），也可用 `--api-key` 传。

退出码
------
0 = 全部成功；1 = 有题目失败；2 = 前置不满足（无 key / 连不上 / 题集为空）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from scripts.d21_cases import _A_CASES

# ---------------------------------------------------------------- 常量

DEFAULT_BASE_URL = "http://localhost/v1"
USER_ID = "d39-eval"
HTTP_TIMEOUT_S = 240.0
MAX_RETRIES = 2
OUT_DIR = Path("data")

# ★ 必须与 app/services/eval_runner.py:70 的 _NO_CHUNKS **逐字一致**。
#   两侧 chunks 文本同构，是「同一 judge 可比」的前提之一。
NO_CHUNKS = "（本轮没有检索到任何片段）"

# ★ Dify 侧检索配置 —— 2026-10-10 从 Dify 库 `app_model_configs.dataset_configs` 实测登记。
#   报告（docs/eval-compare-*.md）的免责声明直接引用这份，避免手抄写错。
#   ⚠ 在 Dify 界面改过任何检索参数后，**必须同步改这里**，否则报告在撒谎。
DIFY_SIDE_CONFIG: dict[str, Any] = {
    "app_name": "agentforge-qa",
    "app_id": "8f254024-af06-4f2c-a5a7-b4e1f7b21efe",
    "dataset_name": "agentforge-corpus-11",
    "dataset_id": "37c5db13-bec6-4913-aea9-61856467baeb",
    "corpus": "11 篇（与 AgentForge 同源）",
    "chunks_in_dify": 88,
    "chunks_in_agentforge": 82,
    "indexing_technique": "high_quality",
    "embedding_model": "bge-m3（1024 维，Ollama）",
    "retrieval_model": "multiple（多路召回 = 向量 + 全文）",
    "top_k": 4,
    "reranking_mode": "weighted_score",
    "vector_weight": 0.7,
    "keyword_weight": 0.3,
    "score_threshold_enabled": False,
    "rerank_model": None,
    "generation_model": "deepseek-flash",
    "known_limits": [
        "本机无 rerank 模型 → Dify 以「无重排」参赛，"
        "AgentForge 侧重排是进程内 TextCrossEncoder(bge-reranker-base)，两侧不对等",
        "嵌入不同源：AgentForge vector(512) vs Dify bge-m3(1024) → score 量纲不可比",
        "同源入库但非逐片一致：AgentForge 11 篇切 82 片 vs Dify 88 块（1.07x）；"
        "另有 6 字符差异（2 篇 .md 丢首行 '# ' + 若干换行），归因未锁定",
    ],
}

# <think>…</think>；DOTALL 让 . 跨行，IGNORECASE 兼容 <Think>
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_REASONING_MARK = "<!--dify-deepseek-reasoning-->"


# ---------------------------------------------------------------- 环境


def load_dotenv(path: Path = Path(".env")) -> None:
    """把 .env 里的键值灌进 os.environ（**不覆盖**已存在的）。

    为什么不引 python-dotenv：只需要这一件事，自己写十来行就够，少一个依赖。
    pydantic-settings 那条路只管 `app.config`，命令行脚本读不到 —— 这里补上。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- 纯函数


def strip_think(text: str) -> tuple[str, bool]:
    """剥掉 <think> 块，返回 (干净文本, 是否剥到了东西)。

    `had` 是给验收用的 —— 如果实测一条都没剥到，说明模型换了或标记变了，
    这时候「answer 很长」就得重新解释，不能默认「已经干净了」。
    """
    cleaned = _THINK_RE.sub("", text)
    had = cleaned != text
    if _REASONING_MARK in cleaned:
        cleaned = cleaned.replace(_REASONING_MARK, "")
        had = True
    return cleaned.strip(), had


def build_chunks(resources: list[dict[str, Any]]) -> str:
    """按 AgentForge 的模板把检索结果拼成 chunks 文本。

    模板：`[n] 来源：<文档名>\n<内容>`，片段之间用空行连接。
    与 `app/services/eval_runner.py::_format_chunks` 逐字符同构。
    """
    return "\n\n".join(
        f"[{i}] 来源：{r.get('document_name')}\n{r.get('content')}"
        for i, r in enumerate(resources, 1)
    ) or NO_CHUNKS


def load_cases(limit: int | None) -> list[dict[str, Any]]:
    """载入 A 组（doc_qa）题目。顺序稳定 → 输出可复现。"""
    cases = list(_A_CASES)
    if not cases:
        raise RuntimeError(
            "A 组题集是空的 —— 检查 scripts/d21_cases.py 的 _A_CASES。"
            "不要跳过这个检查：分母为 0 会被下游当成 0% 报出去。"
        )
    if limit is not None:
        cases = cases[:limit]
    return cases


# ---------------------------------------------------------------- 网络


async def fetch_app_info(client: httpx.AsyncClient) -> dict[str, Any]:
    """GET /info —— 顺带当 key 有效性探针。"""
    resp = await client.get("/info")
    resp.raise_for_status()
    return resp.json()


async def ask_once(client: httpx.AsyncClient, question: str) -> dict[str, Any]:
    """单题单轮作答。返回 {answer, resources, latency, usage} 或抛错。"""
    payload = {
        "inputs": {},
        "query": question,
        "response_mode": "blocking",
        "user": USER_ID,
        # 刻意不传 conversation_id：单轮独立，题与题之间不互相污染
    }
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.post("/chat-messages", json=payload)
            resp.raise_for_status()
            data = resp.json()
            meta = data.get("metadata") or {}
            return {
                "answer_raw": data.get("answer", ""),
                "resources": meta.get("retriever_resources") or [],
                "latency_s": (meta.get("usage") or {}).get("latency"),
                "usage": meta.get("usage"),
            }
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                exc.response.status_code in {429, 500, 502, 503, 504}
            )
            if attempt < MAX_RETRIES and retryable:
                wait = 2 * (attempt + 1)
                print(f"      ↻ 第 {attempt + 1} 次失败（{type(exc).__name__}），{wait}s 后重试")
                await asyncio.sleep(wait)
                continue
            break
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------- 主流程


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


async def run(args: argparse.Namespace) -> int:
    base_url = args.base_url or os.environ.get("DIFY_BASE_URL", DEFAULT_BASE_URL)

    section("1/3 载入 A 组题集")
    try:
        cases = load_cases(args.limit)
    except RuntimeError as exc:
        print(f"  ❌ {exc}")
        return 2
    print(f"  A 组 doc_qa：{len(cases)} 条" + (f"（--limit {args.limit}）" if args.limit else ""))

    if args.dry_run:
        for case in cases:
            print(f"  {case['case_key']}  [{case.get('difficulty', '?'):6s}] {case['question'][:56]}")
        print("\n  （dry-run：未发任何请求）")
        return 0

    api_key = args.api_key or os.environ.get("DIFY_APP_API_KEY", "")
    if not api_key:
        print("\n  ❌ 缺少 app key：请设 DIFY_APP_API_KEY（见 .env）或传 --api-key")
        return 2

    section("2/3 逐题向 Dify 发问")
    out_path = Path(args.out) if args.out else OUT_DIR / f"dify_answers_{datetime.now():%Y-%m-%d}.json"

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=HTTP_TIMEOUT_S,
        # 本机有代理在应答时，不加这个会拿到"看起来像服务挂了"的假状态码
        trust_env=False,
    ) as client:
        try:
            info = await fetch_app_info(client)
        except httpx.HTTPStatusError as exc:
            print(f"  ❌ key 无效或无权限：HTTP {exc.response.status_code}")
            return 2
        except httpx.HTTPError as exc:
            print(f"  ❌ 连不上 {base_url}：{type(exc).__name__}: {exc}")
            return 2
        print(f"  应用：{info.get('name')}  ·  mode={info.get('mode')}")

        results: list[dict[str, Any]] = []
        for i, case in enumerate(cases, 1):
            key = case["case_key"]
            q = case["question"]
            print(f"\n  [{i}/{len(cases)}] {key}  {q[:52]}")
            row: dict[str, Any] = {
                "case_key": key,
                "category": case.get("category"),
                "difficulty": case.get("difficulty"),
                "question": q,
                "reference": case.get("reference"),
                "doc_slugs": case.get("doc_slugs") or [],
                "ok": False,
                "error": None,
            }
            try:
                raw = await ask_once(client, q)
            except Exception as exc:  # noqa: BLE001 —— 单题失败不该中断整轮
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(f"      ❌ {row['error'][:110]}")
                results.append(row)
                continue

            clean, stripped = strip_think(raw["answer_raw"])
            resources = raw["resources"]
            docs = [r.get("document_name") for r in resources]
            scores = [r.get("score") for r in resources]

            row.update(
                {
                    "ok": True,
                    "answer": clean,
                    "chunks": build_chunks(resources),
                    "chunk_count": len(resources),
                    "retrieved_docs": docs,
                    "retrieved_scores": scores,
                    "answer_chars_raw": len(raw["answer_raw"]),
                    "answer_chars_clean": len(clean),
                    "thinking_stripped": stripped,
                    "latency_s": raw.get("latency_s"),
                    "usage": raw.get("usage"),
                }
            )

            # 防御：剥完变空 = 全文都是思考块，这种答案不能拿去打分
            if not clean and raw["answer_raw"]:
                row["error"] = "剥 <think> 后 answer 为空（可能整段都是思考块）"
                row["ok"] = False
                print("      ⚠ 剥完为空 —— 已标记失败")

            flag = "✅" if row["ok"] else "❌"
            print(
                f"      {flag} 答案 {len(raw['answer_raw'])}→{len(clean)} 字符"
                f"（{'已剥 think' if stripped else '无 think'}）"
                f" · 召回 {len(resources)} 条 · {raw.get('latency_s') and round(raw['latency_s'], 1)}s"
            )
            for n, (d, s) in enumerate(zip(docs, scores), 1):
                print(f"         [{n}] {d}  score={s}")
            if not resources:
                print("         ⚠ 召回为空 —— 该题 MRR/NDCG 分母会缺项，报告须如实说明")

            results.append(row)

    section("3/3 落盘")
    ok_rows = [r for r in results if r["ok"]]
    doc = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": base_url,
            "user_id": USER_ID,
            "case_group": "A (doc_qa)",
            "case_count": len(results),
            "ok_count": len(ok_rows),
            "chunks_template": "[n] 来源：<document_name>\\n<content>（与 eval_runner._format_chunks 同构）",
            "no_chunks_placeholder": NO_CHUNKS,
            "dify_side_config": DIFY_SIDE_CONFIG,
        },
        "results": results,
    }
    # 同一维度出现两个 chunks 来源会污染结论（同一 judge 的前提之一）
    assert doc["meta"]["no_chunks_placeholder"] == "（本轮没有检索到任何片段）", "占位符被改动"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    total_chunks = sum(r.get("chunk_count") or 0 for r in ok_rows)
    empty_retrieval = [r["case_key"] for r in ok_rows if not r.get("chunk_count")]
    print(f"  题数：{len(results)}（成功 {len(ok_rows)} / 失败 {len(results) - len(ok_rows)}）")
    print(f"  召回片段合计：{total_chunks} · 平均 {total_chunks / max(len(ok_rows), 1):.1f} 条/题")
    print(f"  剥 <think>：{sum(1 for r in ok_rows if r.get('thinking_stripped'))} 条命中")
    if empty_retrieval:
        print(f"  ⚠ 零召回：{', '.join(empty_retrieval)}")
    print(f"  产出：{out_path}")

    if len(ok_rows) != len(results):
        failed = [r["case_key"] for r in results if not r["ok"]]
        print(f"\n  ❌ 失败题：{', '.join(failed)}")
        return 1

    print("\n  下一步：scripts/eval_compare.py 读本文件 + AgentForge 侧结果 → 同一 judge")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="D39：把 A 组评测题发给 Dify 应用，取回答案与 chunk 级检索结果"
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（试跑用）")
    parser.add_argument("--dry-run", action="store_true", help="只列题，不发请求")
    parser.add_argument("--api-key", default=None, help="Dify 应用 Service API key（默认读 .env）")
    parser.add_argument("--base-url", default=None, help=f"默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--out", default=None, help="输出 JSON 路径（默认 data/dify_answers_<date>.json）")
    args = parser.parse_args()

    load_dotenv()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
