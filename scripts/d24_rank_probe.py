"""
D24 名次探针：改问法到底能不能把 gold 从第 1 名挤下来 —— 零 LLM 调用
=====================================================================

判据从哪来
----------
D23 测出三配置在检索层全是天花板（Hit@1/5/10 全 100%、MRR 恒 1.000）。
但"天花板"本身不解释**为什么**。D24 开工前的三个零 token 探针把原因钉死了：

  用最笨的打分（只数共享词个数）→ **32/35 条题 gold 直接排第 1 名**，
  领先优势中位只有 **3 个词**；gold 的优势全靠**主题词**撑着。

本脚本量的是同一件事的**精确形式** —— gold 在各条检索链路上的名次：

    vec     纯向量路（`search`）          —— 语义召回
    bm25    纯词法路（`search_keywords`） —— 关键词召回
    fused   两路 RRF 融合（`hybrid_search`）—— B 配置实际用的顺序
    rerank  融合 top20 → 重排（`retrieve` 走 hybrid_rerank）—— C 配置实际用的顺序

--------------------------------------------------------------------------
为什么名次比"命中率"有用（本脚本存在的全部理由）
--------------------------------------------------------------------------
`retrieve()` 默认取 top5（`DEFAULT_TOP_K = 5`），所以：

    gold 名次 1~5   → B 拿得到 → **B 与 C 必然一样**（C 顶多把它排得更好看）
    gold 名次 6~20  → B 拿不到、**C 的窗口是 20，还有机会救回来** ← 唯一能分开 B/C 的区间
    gold 名次 >20   → 重排窗口之外，**谁都拿不到** → 只是"变难"，不产生任何差异

所以「题太简单」这句话的正确形式是：**现在满足 6~20 的题有 0 条**。
本脚本量的就是这个数 —— 它决定改问法这条路走不走得通。

⚠ 要分开 A（pure_vector）与 B（hybrid），还需要另一个条件：
  **`vec` 名次 ≥6 而 `fused` 名次 ≤5** —— 即"向量单独做不到、融合补上了"。
  本脚本把 `vec` 也一并打出来，就是为了能当场看这个条件。

--------------------------------------------------------------------------
成本
--------------------------------------------------------------------------
**零 token**：四条链路都不调 LLM，也不调 judge。只有 `rerank` 会用本地
ONNX 模型（bge-reranker-base）做一次前向，20 条题约十几秒。
同一份语料重跑结果逐字相同（检索是确定性的）。

用法（项目根目录）：
    uv run python -m scripts.d24_rank_probe
    uv run python -m scripts.d24_rank_probe --top-k 30
"""

import argparse
import asyncio
import sys
from collections import Counter

PROJECT_ROOT = "/Users/779369901qq.com/workspace/bs/AgentForge-Lite"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app.models  # noqa: F401 —— 导入即注册全部模型
from sqlalchemy import text

from app.core.db import engine
from app.services.retrieval_service import (
    hybrid_search,
    retrieve,
    search,
    search_keywords,
)
from scripts.d21_corpus import SLUG_TO_FILENAME
from scripts.d24_cases_challenge import CHALLENGE_CASES

# 探测窗口。必须 ≥ 重排窗口（RERANK_CANDIDATE_K = 20），
# 否则"名次 20 以外"这种最该被看见的情况会被截断成"没命中"，两者混在一起。
DEFAULT_PROBE_K = 20

# 判据的两个边界 —— 抽成常量是因为它们会出现在结论文字里，
# 写死在两处就是两份真相（同 d23_retrieval_metrics 的 CONFIGS 那条理由）
B_CONFIG_TOP_K = 5          # B 配置实际取的条数（retrieval_service.DEFAULT_TOP_K）
RERANK_WINDOW = 20          # C 配置的重排窗口（retrieval_service.RERANK_CANDIDATE_K）


async def slug_to_uuid(conn) -> dict[str, object]:
    """
    doc_slug → document UUID。

    `documents` 表里**没有** slug 列（实测 information_schema：只有 id / filename /
    file_type / status / chunk_count / error_message / created_at）——
    slug 是出题侧的业务标识，靠 `SLUG_TO_FILENAME` 反查文件名再对 UUID。
    ⚠ 这条是回查真实结构得到的，不是按印象写的列名（第一次写成 `d.doc_slug`，报 UndefinedColumn）。
    """
    rows = (
        await conn.execute(text("select id, filename from documents"))
    ).mappings().all()
    by_filename = {r["filename"]: r["id"] for r in rows}
    out: dict[str, object] = {}
    for slug, filename in SLUG_TO_FILENAME.items():
        if filename in by_filename:
            out[slug] = by_filename[filename]
    return out


async def chunks_to_docs(conn, hits: list[dict]) -> dict[str, object]:
    """切片 id → 所属文档 UUID（检索结果里只有 chunk_id，没有文档身份）。"""
    ids = [h["chunk_id"] for h in hits if h.get("chunk_id") is not None]
    if not ids:
        return {}
    rows = (
        await conn.execute(
            text("select id, document_id from document_chunks where id = any(:ids)"),
            {"ids": [int(x) for x in ids]},
        )
    ).mappings().all()
    return {str(r["id"]): r["document_id"] for r in rows}


def gold_rank(hits: list[dict], chunk2doc: dict, gold_uuids: set) -> int | None:
    """
    第一个属于 gold 的**文档**的名次（1 起）；没命中返回 None。

    为什么要做**文档级**而不是切片级：一篇文档有很多切片，
    切片级名次会被"同一篇命中好几片"稀释成一个虚高的名次，
    而这个名次要拿去和 `DEFAULT_TOP_K`（条数）比 —— 两个口径必须一致。
    取**最早**名次（`setdefault` 语义）：名次是"用户要翻到第几条才看到答案"。
    """
    best: dict = {}
    for rank, h in enumerate(hits, start=1):
        did = chunk2doc.get(str(h.get("chunk_id")))
        if did is not None:
            best.setdefault(did, rank)
    ranks = [r for d, r in best.items() if d in gold_uuids]
    return min(ranks) if ranks else None


async def measure(conn, question: str, gold_uuids: set, k: int) -> dict:
    """
    一条题 × 四条链路 → 四个名次。

    ⚠ 四条链路各自独立跑一遍，**不复用**任何一方的结果 ——
      复用会让"某条链路其实没跑到"这种情况看不出来
      （本探针的全部价值就在于区分"哪条链路把它排到哪"）。
    """
    out: dict = {}
    for label, fn in (
        ("vec", lambda q: search(q, top_k=k)),
        ("bm25", lambda q: search_keywords(q, top_k=k)),
        ("fused", lambda q: hybrid_search(q, top_k=k)),
        ("rerank", lambda q: retrieve(q, top_k=k, config="hybrid_rerank")),
    ):
        try:
            hits = await fn(question)
        except Exception as e:  # noqa: BLE001 —— 单条链路失败不该中断整轮
            out[label] = None
            out[f"{label}_err"] = f"{type(e).__name__}: {e}"
            continue
        c2d = await chunks_to_docs(conn, hits)
        out[label] = gold_rank(hits, c2d, gold_uuids)
    return out


def verdict(fused: int | None, vec: int | None) -> str:
    """
    按名次给这条题贴标签 —— 用词由数据决定，不硬编码（铁律 11）。

    三个标签的含义（"能分开谁"是这张表最重要的信息）：
      天花板      fused ≤5  → B 就拿到了，C 不可能比 B 差在这一层 → 分不开 B/C
      可分开 B/C  fused ∈ 6~20 → B 拿不到、C 的窗口够 → 唯一有价值的区间
      谁都拿不到  fused >20 或未命中 → 重排窗口之外，只变难不产生差异
    """
    if fused is None:
        return "谁都拿不到"
    if fused <= B_CONFIG_TOP_K:
        return "天花板"
    if fused <= RERANK_WINDOW:
        return "可分开 B/C"
    return "谁都拿不到"


def ab_contrast(fused: int | None, vec: int | None) -> str:
    """
    A（pure_vector，取 top5）与 B（hybrid，取融合序 top5）**会不会拿到不同结果**。

    ⚠ 这个函数的第一版只写了一个方向（"向量名次差、融合名次好" → 判为能分开），
      实测 D24 时被抓出来：那条题是**反的** —— 向量第 3 名（A 拿得到）、
      融合第 8 名（B 拿不到），却被标成"向量已达到"，等于把一条真区分当成没区分。
      → 教训：**"能不能分开"是对称的，只判一个方向 = 把一半的答案丢掉，
        而且丢掉的那一半在输出里长得像"没问题"。**

    四象限（命中 = 名次 ≤ top_k）：
        A 中 / B 不中  → 能分开（A 中 B 不中）   ← 融合把强信号拉低了
        A 不中 / B 中  → 能分开（B 中 A 不中）   ← 融合把弱信号救回来了
        A 中 / B 中    → 分不开（都中）          ← 现在 35/35 全在这一档
        A 不中 / B 不中 → 分不开（都不中）
    """
    if fused is None or vec is None:
        return "—（有链路没命中）"
    a_hit = vec <= B_CONFIG_TOP_K
    b_hit = fused <= B_CONFIG_TOP_K
    if a_hit and not b_hit:
        return "能分开（A 中 B 不中）"
    if b_hit and not a_hit:
        return "能分开（B 中 A 不中）"
    if a_hit and b_hit:
        return "分不开（都中）"
    return "分不开（都不中）"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=DEFAULT_PROBE_K,
                    help=f"探测窗口（默认 {DEFAULT_PROBE_K}，必须 ≥ {RERANK_WINDOW}）")
    args = ap.parse_args()
    k = args.top_k
    if k < RERANK_WINDOW:
        # 不拦的话，"名次 20 以外"和"没命中"会印成同一个样子，结论会被悄悄改写
        print(f"--top-k 必须 ≥ {RERANK_WINDOW}（重排窗口），收到 {k} —— 拒绝出结果。")
        return

    # 对照组题目从**源文件**读，不从库里读：
    # 库里那份将来可能被重跑/重建，源文件才是"这批题的定义"
    from scripts.d21_cases import CASES as BASE_CASES

    base_by_key = {c["case_key"]: c for c in BASE_CASES}

    async with engine.connect() as conn:
        s2u = await slug_to_uuid(conn)
        missing = sorted({s for c in CHALLENGE_CASES for s in c["doc_slugs"]} - set(s2u))
        if missing:
            print(f"⚠ 语料里找不到这些 slug 对应的文档：{missing}")
            print("  先跑 scripts/d21_seed.py 重建语料 —— 本探针不下任何结论。")
            await engine.dispose()
            return

        print(f"探测窗口 top_k = {k}（B 配置实际只取前 {B_CONFIG_TOP_K} 条；"
              f"C 配置重排窗口 {RERANK_WINDOW}）")
        print(f"挑战题 {len(CHALLENGE_CASES)} 条，各自配一条对照题\n")

        rows: list[dict] = []
        for ch in CHALLENGE_CASES:
            ctrl = base_by_key[ch["control_case_key"]]
            gold = {s2u[s] for s in ch["doc_slugs"]}
            print(f"  跑 {ch['case_key']}（对照 {ctrl['case_key']}）…", end=" ", flush=True)
            ctrl_m = await measure(conn, ctrl["question"], gold, k)
            ch_m = await measure(conn, ch["question"], gold, k)
            print("ok")
            rows.append({"ch": ch, "ctrl": ctrl, "ctrl_m": ctrl_m, "ch_m": ch_m})

    # ============================================================
    # ① 逐题并排
    # ============================================================
    print()
    print("=" * 108)
    print("① 逐题并排：同一批答案，换个问法之后 gold 排到第几名")
    print("=" * 108)
    print(f"{'题号':<6}{'对照':<6}{'向量':>6}{'词法':>6}{'融合':>6}{'重排':>6}   {'判定':<12}{'A vs B':<20}")
    for r in rows:
        m = r["ch_m"]
        print(
            f"{r['ch']['case_key']:<6}{r['ctrl']['case_key']:<6}"
            f"{str(m['vec']):>6}{str(m['bm25']):>6}{str(m['fused']):>6}{str(m['rerank']):>6}   "
            f"{verdict(m['fused'], m['vec']):<12}{ab_contrast(m['fused'], m['vec']):<20}"
        )
    print()
    print(f"{'题号':<6}{'对照':<6}{'向量':>6}{'词法':>6}{'融合':>6}{'重排':>6}   （易问法，即原题）")
    for r in rows:
        m = r["ctrl_m"]
        print(
            f"{r['ctrl']['case_key']:<6}{'—':<6}"
            f"{str(m['vec']):>6}{str(m['bm25']):>6}{str(m['fused']):>6}{str(m['rerank']):>6}"
        )

    # ============================================================
    # ② 分布：有没有题落进"能分开 B/C"的区间
    # ============================================================
    print()
    print("=" * 108)
    print("② 分布 —— 这是本探针唯一要回答的问题")
    print("=" * 108)
    ch_fused = [r["ch_m"]["fused"] for r in rows]
    ctrl_fused = [r["ctrl_m"]["fused"] for r in rows]
    n = len(rows)

    def dist(vals: list[int | None]) -> Counter:
        c: Counter = Counter()
        for v in vals:
            if v is None:
                c["未命中"] += 1
            elif v <= B_CONFIG_TOP_K:
                c[f"1~{B_CONFIG_TOP_K}（天花板）"] += 1
            elif v <= RERANK_WINDOW:
                c[f"{B_CONFIG_TOP_K + 1}~{RERANK_WINDOW}（可分开 B/C）"] += 1
            else:
                c[f">{RERANK_WINDOW}（谁都拿不到）"] += 1
        return c

    print("挑战题（难问法）：")
    for kk, v in sorted(dist(ch_fused).items()):
        print(f"  {kk:<24}{v}/{n}")
    print("对照组（易问法）：")
    for kk, v in sorted(dist(ctrl_fused).items()):
        print(f"  {kk:<24}{v}/{n}")

    n_split_bc = sum(1 for v in ch_fused if v is not None and B_CONFIG_TOP_K < v <= RERANK_WINDOW)
    n_split_ab = sum(
        1
        for r in rows
        if ab_contrast(r["ch_m"]["fused"], r["ch_m"]["vec"]).startswith("能分开")
    )
    n_ctrl_split_bc = sum(
        1 for v in ctrl_fused if v is not None and B_CONFIG_TOP_K < v <= RERANK_WINDOW
    )

    print()
    print(f"挑战题里落进「可分开 B/C」区间的：{n_split_bc}/{n}"
          f"（对照组是 {n_ctrl_split_bc}/{n}）")
    print(f"挑战题里 A 与 B 会拿到**不同结果**的：{n_split_ab}/{n}")

    # —— 结论由数据分支决定 ——
    print()
    if n_split_bc == 0:
        print("→ 结论：**改问法没能在任何一条题上把 gold 挤出 top5**。")
        print("  也就是说「换个说法问」这条路在这份语料上不足以造出区分度，")
        print("  下一步应当考虑「加同主题干扰文档」（乙案）—— 那是从语料侧造竞争。")
    elif n_split_bc >= max(3, n // 3):
        print(f"→ 结论：**改问法有效** —— {n_split_bc}/{n} 条的 gold 落进了融合序 {B_CONFIG_TOP_K + 1}~{RERANK_WINDOW} 名。")
        print("  这批题值得扩到全量，然后才跑三配置重跑（那一步才花 token）。")
    else:
        print(f"→ 结论：**改问法部分有效** —— 只有 {n_split_bc}/{n} 条落进目标区间。")
        print("  够不够撑起一次有区分度的消融，要看扩到全量后能拿到多少条；")
        print("  若扩完仍不足，需要叠加乙案（加同主题干扰文档）。")

    # ============================================================
    # ③ 自检（防「空列表恒满足」）
    # ============================================================
    print()
    print("=" * 108)
    print("③ 自检")
    print("=" * 108)
    errs = [(r["ch"]["case_key"], kk) for r in rows for kk, v in r["ch_m"].items()
            if kk.endswith("_err") and v]
    print(f"链路报错的题：{errs or '无'}")
    all_none = all(r["ch_m"]["fused"] is None for r in rows)
    print(f"挑战题的融合名次**全为 None** 吗？{all_none}   ← 应为 False（全 None 说明检索根本没跑起来）")
    never1 = all(r["ctrl_m"]["fused"] != 1 for r in rows)
    print(f"对照组**没有任何一条** gold 排第 1 吗？{never1}   ← 应为 False（对照组是已知的天花板）")
    print(f"对照组 gold 融合名次分布：{sorted(v for v in ctrl_fused if v is not None)}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
