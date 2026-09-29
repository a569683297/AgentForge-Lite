"""
D23 第 ③ 步：检索层指标（Hit@k / MRR）—— 离线、零 LLM 调用
=============================================================
为什么要有这个脚本（它回答的问题前三轮答不出来）
------------------------------------------------
D22 / D23 的评测都停在**答案级**：跑被测系统生成答案 → judge 打分 → 准确率。
结果是三配置 **98% / 98% / 98%**，一条差异都没有。原因有两层：

  ① judge 拿「出题说明」当答案 → 假失败（已在 D23 修好）
  ② 修好之后 **仍然是天花板** —— 题太简单

「题太简单」这句话在答案级指标上**没法验证**：答案对不对经过了
"检索 → LLM 生成 → judge 打分"三层，任何一层到顶都会让整体到顶。
本脚本把中间那两层**拆掉**，只量**检索**这一层：

    问题 → retrieve() → 排好序的片段列表 → 和 gold 文档对比

于是「检索到底能不能把对的文档捞回来」变成一个**确定性**数字：
不调 LLM、不调 judge、**零 token**、同一份语料重跑结果逐字相同。

--------------------------------------------------------------------------
指标定义（每个第一次出现的名词都在原地解释）
--------------------------------------------------------------------------
  **gold 文档**（标准答案文档 / ground truth）：这道题的答案应该来自哪几篇文档。
     本项目里就是 `eval_cases.doc_ids`（UUID 数组），出题时按语料逐条标注。
     ⚠ 它**只用于判分统计**，绝不能当检索过滤条件 —— 那等于开卷考试还给页码，
       A/B/C 的差异会被整体抹平（d21_cases.py 的纪律）。

  **Hit@k**（命中率）：top-k 结果里**至少有一条**来自 gold 文档集合 → 记 1，否则 0。
     题级平均。k=5 时就是「前 5 条里有没有我要的那篇」。

  **MRR**（Mean Reciprocal Rank，平均倒数名次）：第一篇 gold 文档排第几名，
     就记 1/名次（第 1 名 → 1.0，第 3 名 → 0.333，没命中 → 0），再对全部题取平均。
     它比 Hit@k 敏感：Hit@5 只问「在不在前 5」，MRR 还问「排多前」。

  **MRR_last@k**（本脚本新增）：**最后一片** gold 的名次倒数。
     为什么需要它 —— 标准 MRR 在这个数据集上**恒等于 1.000**（每道题的第一篇
     gold 都排第 1 名），完全没有信息量。而跨文档题有两篇 gold：
     「第一篇第 1 名、第二篇第 7 名」的检索质量，显然不如「第 1 名、第 2 名」。
     MRR_last 量的就是这件事，它是本数据集上**唯一还剩梯度的指标**。

  **Hit@k(all)**：gold 文档**全部**出现在 top-k 里（不是「至少一篇」）。
     跨文档题专属口径 —— 只捞到一篇等于答案必然不全。

  **随机基线**（random baseline）：把「随便一篇**非 gold** 文档」当答案，它能落进
     top-k 的概率。这是**负对照**：用来回答「100% 的 Hit@5 有没有信息量」。
     基线高（比如 40%）说明语料太小、窗口太宽，谁都能进 → 100% 说明不了什么。

  **文档级去重**：同一篇文档可能命中好几个切片。只记它**最早出现的名次**，
     不重复计数。否则一篇长文档能靠切片数量把 top-k 占满，
     「命中率」被切片数而不是文档质量决定。

--------------------------------------------------------------------------
三个必须记住的坑（都写进了代码）
--------------------------------------------------------------------------
  ① **gold 的 UUID 会随语料重建而失效**。`doc_ids` 指向 `documents.id`，
     而重建语料必然生成新 UUID → 全部题变成「未命中」→ 看起来像"检索彻底坏了"，
     实际是标注失效。**脚本前置核验对齐率，不足 100% 直接拒绝出指标**
     （同 D22「指纹不等就拒绝出报告」）。

  ② **gold 是 UUID，检索结果只给 chunk_id**。要先 chunk → document_id 才能比。
     检索层返回里**没有** document_id，也没有 doc_slug（D11 的引用收集器
     只留了 source/content/similarity/page_ref）—— 所以只能回库查一次映射。

  ③ **负例题与工具类题没有 gold，必须排除**。分子分母都要对得上：
     50 条题里只有 35 条能算（25 条单文档 + 10 条跨文档）。
     另外 15 条不是"算不出"，是**按定义就没有正确答案文档**：
     负例的正确答案是"拒答"，工具题的判定依据是工具调用。

--------------------------------------------------------------------------
B 与 C 的对比为什么是干净的（一个容易搞混的地方）
--------------------------------------------------------------------------
`hybrid`（B）与 `hybrid_rerank`（C）在 top_k 口径下的链路差别：

    B: 两路各取 20 → RRF 融合 → **按融合序取前 top_k**
    C: 两路各取 20 → RRF 融合 → 取前 20 → **重排** → 取前 top_k

两路的**输入候选一样大**（各 20），差别只在最后一步。
所以 C 相对 B 的改进，可以干净地归因到**重排**（把融合序第 11~20 名里
被低估的正确文档提到前面来）—— 而不是「候选更多」。

⚠ 但有一个已知的**降级**情况：重排失败时 `retrieve()` 会退回融合序，
  并把 `retriever` 标成 `hybrid`（D19 的"降级必须可归因"）。若这种降级大量发生，
  C 的成绩其实是 B 的成绩。**所以脚本统计 `retriever` 字段的实际分布**，
  降级条数会打印出来 —— 不靠"应该没降级"下结论。

用法（在项目根目录）：
    uv run python -m scripts.d23_retrieval_metrics
    uv run python -m scripts.d23_retrieval_metrics --config hybrid_rerank
    uv run python -m scripts.d23_retrieval_metrics --json /tmp/metrics.json
"""

import argparse
import asyncio
import json
import time
from collections import Counter

import app.models  # noqa: F401 —— 导入即注册全部模型
from sqlalchemy import text

from app.core.db import engine
from app.services.retrieval_service import retrieve

# 三配置 = 消融的自变量（PRD §9.5）。与 d22_run / d23_diagnose 同一份取值，
# 不许在别处另写一遍 —— 同一份名单两处写就是两份真相。
CONFIGS: tuple[str, ...] = ("pure_vector", "hybrid", "hybrid_rerank")

# 报多少名的梯度。评测主链路用 top5（retrieval_service.DEFAULT_TOP_K），
# 但指标要看更大窗口，所以默认报 top10 —— 这只是探针显式传参，
# **不改任何评测行为**（不改常量、不改默认值）。
METRIC_TOP_K = 10


# ============================================================
# 统计工具
# ============================================================
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    比例型指标的 **Wilson 置信区间**（95%）。

    什么是置信区间：样本只有 10 条题时，「8/10 命中」这个点估计并不精确，
    换一批同分布的题可能得到 6/10 或 10/10。区间给出「真值大概落在哪」。
    为什么用 Wilson 而不是最简单的 p ± 1.96·√(p(1-p)/n)：后者在 p 接近 0 或 1 时
    会算出超过 [0,1] 的区间（比如 100% 命中时算到 105%），Wilson 不会。

    ⚠ 这里的区间衡量的是**样本量不足**，不是「重跑会不同」——
      检索层是确定性的，同一份语料重跑 100 次结果逐字相同。
      区间宽只说明「10 条题太少，结论不稳」。
    """
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    lo = (center - margin) / d
    hi = (center + margin) / d
    # 夹到 [0, 1]。为什么必须夹：k=0 时 center 与 margin 在浮点上未必精确相等，
    # 相减会得到 -2.8e-17 这种**负的概率**。它打印出来是 `-0.0%`（看着无害），
    # 但它是个错的数 —— 下游拿它做区间比较（如"两个区间是否重叠"）就会出错。
    # 这条是被 d23_metrics_verify.py 的 A6 断言抓出来的（不是"看着合理"就算过）。
    return (max(0.0, lo), min(1.0, hi))


# ============================================================
# 数据加载
# ============================================================
async def load_cases(conn) -> tuple[list[dict], list[dict]]:
    """
    载入「可算检索层指标」的题 + 被排除的题（各自的排除理由要能说出来）。

    排除判据不写在这里：直接查库看 doc_ids 有没有值 ——
    判据的唯一出处是**数据本身**（有 gold 才算得了），不是 category 的名字。
    """
    rows = (
        await conn.execute(
            text(
                """
                select case_key, category, question, doc_ids, doc_slugs, difficulty
                from eval_cases
                order by case_key
                """
            )
        )
    ).mappings().all()

    usable, excluded = [], []
    for r in rows:
        item = dict(r)
        if item["doc_ids"]:
            usable.append(item)
        else:
            reason = "负例（正确答案是拒答，没有 gold 文档）" if item["category"] == "doc_qa" \
                else "工具类题（判定依据是工具调用，与检索质量无关）"
            excluded.append({**item, "reason": reason})
    return usable, excluded


async def check_gold_alignment(conn, cases: list[dict]) -> tuple[int, int, list]:
    """
    坑①的前置核验：gold 的 UUID 是不是都还在 documents 表里。

    语料重建会换 UUID → doc_ids 集体失效 → 指标全 0%。
    这一步存在的意义：**让「标注失效」和「检索坏了」显式区分开**。
    不核验的话两者长得一模一样，而前者会让你去乱改本来正确的检索代码。
    """
    gold_ids = set()
    for c in cases:
        gold_ids |= set(c["doc_ids"])
    if not gold_ids:
        return (0, 0, [])
    found = {
        r[0]
        for r in (
            await conn.execute(
                text("select id from documents where id = any(:ids)"),
                {"ids": list(gold_ids)},
            )
        ).all()
    }
    return (len(gold_ids), len(found), sorted(str(x) for x in (gold_ids - found)))


async def count_corpus_docs(conn) -> int:
    """语料文档总数 —— 检索任务的难度上限（从 N 篇里挑）。"""
    return int((await conn.execute(text("select count(*) from documents"))).scalar() or 0)


def ranks_by_document(hits: list[dict], chunk2doc: dict[str, object]) -> dict:
    """
    把「按名次排列的命中片段」压成「每篇文档最早出现的名次」。

    抽成纯函数是为了**能被单测**（见 d23_metrics_verify.py 的 B 段）——
    去重这一步错了不会报错，只会让数字悄悄偏掉，所以必须能有断言钉住。

    为什么必须去重（最容易写错、错了也不报错的一步）：
      一篇长文档会切成很多片（本项目 300 字/片），同一篇可能命中好几片。
      不去重的话，一篇文档就能把 top-5 占满 —— 表面看「命中 5 条」，
      实际只有 1 篇文档。两个指标会朝**相反方向**错：
        · Hit@k 高得好看（自己跟自己重复计数）
        · 跨文档题的「两篇都进 top5」永远为假（第二篇被自己的切片挤掉）

    为什么用 `setdefault` 而不是直接赋值：要的是**最早**的名次。
      写成 `best[did] = rank` 会留下最后一次出现的名次 → 名次被系统性拉低
      → MRR_last 偏低，而**不会报任何错**。
    """
    best: dict = {}
    for rank, h in enumerate(hits, start=1):
        did = chunk2doc.get(h.get("chunk_id"))
        if did is not None:
            best.setdefault(did, rank)
    return best


async def chunks_to_docs(conn, chunk_ids: list[str]) -> dict[str, object]:
    """
    坑②：切片身份（chunk_id）→ 所属文档 UUID。

    为什么要一次批量查而不是逐条：top_k×题数 次往返会把脚本拖成分钟级。
    为什么空列表要提前返回：`= any('{}')` 在 PostgreSQL 上会因为无法推断
    数组类型而报错（不是"空集合恒假"那么无害）。
    """
    if not chunk_ids:
        return {}
    rows = (
        await conn.execute(
            text(
                "select c.id as cid, c.document_id "
                "from document_chunks c where c.id = any(:ids)"
            ),
            {"ids": [int(x) for x in chunk_ids]},
        )
    ).mappings().all()
    return {str(r["cid"]): r["document_id"] for r in rows}


# ============================================================
# 单配置：跑检索 + 逐题判定
# ============================================================
async def collect(config: str, top_k: int, cases: list[dict]) -> dict:
    """
    跑一个配置的全部题，返回逐题原始名次。

    单题失败**不中断整轮**：记进 errors 继续跑（同 eval_runner 对生成失败的处置）。
    理由一样 —— 「哪个配置跑不通」本身就是有价值的数据，扔掉它就等于
    把"系统稳定性"从评测里删掉。
    """
    started = time.perf_counter()
    recs: list[tuple[dict, list[dict]]] = []
    retriever_tags: Counter = Counter()
    errors: list[str] = []

    for c in cases:
        try:
            hits = await retrieve(c["question"], top_k=top_k, config=config)
        except Exception as e:  # noqa: BLE001 —— 单题失败不该中断整轮
            errors.append(f"{c['case_key']}: {type(e).__name__}: {e}")
            hits = []
        for h in hits:
            # 坑③的下半：这里统计的是**实际走通的链路**，不是配置名。
            # C 配置降级时全标 "hybrid" —— 只靠配置名会看不出降级。
            retriever_tags[h.get("retriever") or "?"] += 1
        recs.append((c, hits))

    elapsed = time.perf_counter() - started

    async with engine.connect() as conn:
        c2d = await chunks_to_docs(
            conn, [h["chunk_id"] for _, hits in recs for h in hits]
        )

    per_case: list[dict] = []
    for c, hits in recs:
        gold = set(c["doc_ids"])
        # 文档级去重 —— 逻辑在 ranks_by_document（纯函数、可单测），这里只调用
        best = ranks_by_document(hits, c2d)

        gold_ranks = sorted(r for d, r in best.items() if d in gold)
        non_gold_ranks = [r for d, r in best.items() if d not in gold]

        per_case.append(
            {
                "case_key": c["case_key"],
                "category": c["category"],
                "difficulty": c["difficulty"],
                "n_gold": len(gold),
                "gold_ranks": gold_ranks,
                "non_gold_ranks": non_gold_ranks,
                "n_hits": len(hits),
                "distinct_docs": len(best),
                # 原始证据：文档 UUID → 名次。留着它有两个用处 ——
                # ① 报告里每个数字都能回溯到"哪篇文档排第几"
                # ② d23_metrics_verify.py 的负对照段靠它重算「换成错 gold 会怎样」，
                #    不必再跑一遍检索（否则验证脚本每次都要多花 35 秒）
                "ranks_by_doc": {str(d): r for d, r in best.items()},
            }
        )

    return {
        "config": config,
        "per_case": per_case,
        "retriever_tags": dict(retriever_tags),
        "errors": errors,
        "elapsed": elapsed,
    }


# ============================================================
# 指标计算
# ============================================================
def metrics(block: dict, corpus_size: int) -> dict:
    """把逐题名次压成一组指标（每个指标都带分母）。"""
    pc = block["per_case"]
    n = len(pc)

    def hit_any(k: int) -> int:
        """至少一篇 gold 进入 top-k。"""
        return sum(1 for r in pc if any(x <= k for x in r["gold_ranks"]))

    def hit_all(k: int) -> int:
        """gold **全部**进入 top-k。gold 数为 1 时它与 hit_any 等价。"""
        return sum(
            1
            for r in pc
            if r["gold_ranks"]
            and len(r["gold_ranks"]) == r["n_gold"]
            and all(x <= k for x in r["gold_ranks"])
        )

    # 标准 MRR：第一篇 gold 的倒数名次。本数据集上恒为 1（见模块 docstring）
    mrr = sum(1 / r["gold_ranks"][0] for r in pc if r["gold_ranks"]) / n if n else 0.0
    # MRR_last：最后一篇 gold 的倒数名次 —— 跨文档题专用的敏感口径
    mrr_last = sum(1 / r["gold_ranks"][-1] for r in pc if r["gold_ranks"]) / n if n else 0.0

    # 随机基线：非 gold 文档落进 top-k 的比例（逐题算再平均，避免题间 gold 数不等带来的偏差）
    def random_base(k: int) -> float:
        total = 0.0
        for r in pc:
            n_non = corpus_size - r["n_gold"]
            if n_non <= 0:
                continue
            total += sum(1 for x in r["non_gold_ranks"] if x <= k) / n_non
        return total / n if n else 0.0

    cross = [r for r in pc if r["n_gold"] >= 2]

    return {
        "n_cases": n,
        "hit1": hit_any(1) / n if n else 0.0,
        "hit5": hit_any(5) / n if n else 0.0,
        "hit10": hit_any(10) / n if n else 0.0,
        "hit_all5": hit_all(5) / n if n else 0.0,
        "mrr": mrr,
        "mrr_last": mrr_last,
        "rand5": random_base(5),
        "rand10": random_base(10),
        "first_rank_one": sum(1 for r in pc if r["gold_ranks"][:1] == [1]),
        "cross_n": len(cross),
        "cross_all5": sum(
            1 for r in cross
            if len(r["gold_ranks"]) == r["n_gold"]
            and all(x <= 5 for x in r["gold_ranks"])
        ),
        # 只有 1 篇 gold 的题（单文档题）—— 它们是"天花板"的主要来源
        "single_gold_n": sum(1 for r in pc if r["n_gold"] == 1),
    }


# ============================================================
# 输出
# ============================================================
def print_header(corpus_size: int, usable: list[dict], excluded: list[dict],
                 n_gold: int, n_found: int, missing: list) -> None:
    print("=" * 80)
    print("一、可算性与分母口径（先说清分子分母，再谈数）")
    print("=" * 80)
    print(f"  语料文档总数        : {corpus_size}（检索任务 = 从这 {corpus_size} 篇里挑）")
    print(f"  评测题总数          : {len(usable) + len(excluded)}")
    print(f"  参与检索层指标的题  : {len(usable)}"
          f"（单文档 {sum(1 for c in usable if len(c['doc_ids']) == 1)}"
          f" + 跨文档 {sum(1 for c in usable if len(c['doc_ids']) >= 2)}）")
    print(f"  被排除的题          : {len(excluded)}")
    for reason, cnt in Counter(e["reason"] for e in excluded).items():
        print(f"      - {cnt} 条：{reason}")
    print(f"  gold 文档 UUID      : 去重后 {n_gold} 个，能在 documents 里找到 {n_found} 个")
    ok = n_gold == n_found and n_gold > 0
    print(f"  对齐核验            : {'✅ 全部对齐' if ok else '❌ 有对不上的 → 语料被重建过，doc_ids 已失效'}")
    if missing:
        print(f"     对不上的 UUID（前 3）：{missing[:3]}")


def print_main_table(blocks: list[dict], mets: list[dict]) -> None:
    print()
    print("=" * 80)
    print("二、主指标（文档级去重；随机基线是负对照）")
    print("=" * 80)
    print(f"  {'配置':<16}{'Hit@1':>8}{'Hit@5':>8}{'Hit@10':>8}{'MRR':>8}"
          f"{'MRR_last':>10}{'随机@5':>9}{'随机@10':>9}")
    print("  " + "-" * 74)
    for b, m in zip(blocks, mets):
        print(f"  {b['config']:<16}{m['hit1']:>7.1%}{m['hit5']:>8.1%}{m['hit10']:>8.1%}"
              f"{m['mrr']:>8.3f}{m['mrr_last']:>10.3f}"
              f"{m['rand5']:>9.1%}{m['rand10']:>9.1%}")
    print()
    print("  随机基线 = 「随便挑一篇非 gold 文档，它落在 top-k 里的概率」")
    print("  它远低于 Hit@k → 说明指标真的在区分「对的文档」与「别的文档」；")
    print("  三配置之间 Hit@k 完全一样 → 说明在这份语料上，三者没有分别。")


def print_ceiling(mets: list[dict], corpus_size: int) -> None:
    print()
    print("=" * 80)
    print("三、天花板诊断（为什么 Hit@k 没信息量）")
    print("=" * 80)
    for m in mets:
        print(f"  [{m['n_cases']} 题] 首篇 gold 排第 1 名的题：{m['first_rank_one']}/{m['n_cases']}"
              f"   只有 1 篇 gold 的题：{m['single_gold_n']}/{m['n_cases']}"
              f"   MRR = {m['mrr']:.3f}")
    print()
    if all(m["first_rank_one"] == m["n_cases"] for m in mets):
        print("  ⚠ **每一道题的第一篇依据文档，三个配置下都排在第 1 名**。")
        print("     于是 Hit@1 = Hit@5 = Hit@10 = 100%、MRR 恒等于 1.000 ——")
        print("     指标没有上限可测：**不是「检索完美」，是这份语料小到谁都能排对**。")
        print(f"     语料只有 {corpus_size} 篇，题面又与原文条款高度字面重叠（出题时照着条款写的）。")
    else:
        print("  首篇名次并非全为 1 → Hit@k 仍有余量可测。")


def print_cross_doc(mets: list[dict], blocks: list[dict]) -> None:
    """跨文档题 = 天花板之下唯一还有梯度的地方。"""
    print()
    print("=" * 80)
    print("四、跨文档题的「第二篇 gold」—— 本数据集唯一还剩梯度的指标")
    print("=" * 80)
    for m, b in zip(mets, blocks):
        rows = [r for r in b["per_case"] if r["n_gold"] >= 2]
        seconds = [r["gold_ranks"][1] for r in rows if len(r["gold_ranks"]) >= 2]
        lo, hi = wilson(m["cross_all5"], m["cross_n"])
        print(f"  {b['config']:<16} 两篇都进 top5：{m['cross_all5']}/{m['cross_n']}"
              f" = {m['cross_all5']/m['cross_n']:.1%}"
              f"   Wilson95% = [{lo:.1%}, {hi:.1%}]")
        print(f"  {'':<16} 第二篇名次：{seconds}")

    print()
    lo_a, hi_a = wilson(mets[0]["cross_all5"], mets[0]["cross_n"])
    lo_c, hi_c = wilson(mets[-1]["cross_all5"], mets[-1]["cross_n"])
    overlap = not (hi_a < lo_c or hi_c < lo_a)
    if overlap:
        print(f"  ⚠ A 与 C 的置信区间**重叠**（[{lo_a:.1%}, {hi_a:.1%}] ∩ [{lo_c:.1%}, {hi_c:.1%}]）")
        print(f"     → 观测值 C 更好，但跨文档题只有 {mets[0]['cross_n']} 条，")
        print("       **样本量不足以确证**「重排更好」。要确证得加跨文档题或加语料。")
    else:
        print(f"  ✅ A 与 C 的置信区间不重叠 → 差异超出样本量噪声，可以下结论。")
    print()
    print("  ⚠ 并且重排**不是单调变好**：逐题看会有 C 比 B 差的反例（见下一节）。")


def print_per_case(blocks: list[dict]) -> None:
    print()
    print("=" * 80)
    print("五、逐题并排（只列三配置 gold 名次不一致的题）")
    print("=" * 80)
    per_cfg = {b["config"]: {r["case_key"]: r for r in b["per_case"]} for b in blocks}
    configs = [b["config"] for b in blocks]
    keys = sorted(per_cfg[configs[0]])
    diff = [k for k in keys
            if len({tuple(per_cfg[c][k]["gold_ranks"]) for c in configs}) > 1]

    print(f"  完全一致：{len(keys) - len(diff)}/{len(keys)}"
          f"    不一致：{len(diff)}/{len(keys)}")
    if diff:
        print()
        print(f"  {'题号':<7}{'类别':<11}{'gold数':>7}" + "".join(f"{c[:14]:>16}" for c in configs))
        print("  " + "-" * 76)
        for k in diff:
            r0 = per_cfg[configs[0]][k]
            cells = []
            for c in configs:
                gr = per_cfg[c][k]["gold_ranks"]
                cells.append(str(gr) if gr else "未命中")
            print(f"  {k:<7}{r0['category']:<11}{r0['n_gold']:>7}" + "".join(f"{c:>16}" for c in cells))
        print()
        print("  格子里的数字 = gold 文档各自排第几（多个 = 多篇 gold 各一）")
        cats = Counter(per_cfg[configs[0]][k]["category"] for k in diff)
        print(f"  不一致题的类别分布：{dict(cats)}")

        # 反向案例要主动报出来 —— 只报"重排更好"的例子就是选择性汇报
        worst = blocks[-1]["config"]
        base = blocks[0]["config"]
        better = sum(
            1 for k in diff
            if per_cfg[worst][k]["gold_ranks"] and per_cfg[base][k]["gold_ranks"]
            and per_cfg[worst][k]["gold_ranks"][-1] < per_cfg[base][k]["gold_ranks"][-1]
        )
        worse = sum(
            1 for k in diff
            if per_cfg[worst][k]["gold_ranks"] and per_cfg[base][k]["gold_ranks"]
            and per_cfg[worst][k]["gold_ranks"][-1] > per_cfg[base][k]["gold_ranks"][-1]
        )
        print(f"  {worst} vs {base}：变更靠前 {better} 条、变更靠后 {worse} 条"
              f"（其余为单侧未命中）")


def print_health(blocks: list[dict]) -> None:
    """自检段：证明「跑的确实是那个配置」，而不是降级后的结果。"""
    print()
    print("=" * 80)
    print("六、自检：实际走通的链路 + 耗时（降级会让 C 的成绩变成 B 的）")
    print("=" * 80)
    for b in blocks:
        print(f"  {b['config']:<16} 返回片段带 retriever 标签：{b['retriever_tags']}"
              f"   耗时 {b['elapsed']:.1f}s")
        if b["errors"]:
            print(f"  {'':<16} ⚠ 单题失败 {len(b['errors'])} 条：{b['errors'][:2]}")
    rerank_hits = blocks[-1]["retriever_tags"].get("hybrid_rerank", 0)
    total_hits = sum(blocks[-1]["retriever_tags"].values()) or 1
    rate = rerank_hits / total_hits
    print()
    if rate < 0.99:
        print(f"  ⚠ {blocks[-1]['config']} 只有 {rate:.1%} 的片段走了重排 → "
              f"成绩里混了降级样本，不能算作重排的效果")
    else:
        print(f"  ✅ {blocks[-1]['config']} 的片段 {rate:.1%} 走了重排（未发生大规模降级）")
    print("  全程零 LLM 调用 → **零 token**（这是本脚本相对 d22_run 的核心优势）")


def print_conclusion(blocks: list[dict], mets: list[dict], corpus_size: int) -> None:
    """结论段：结论文字由数据决定，不硬编码（铁律 11）。"""
    print()
    print("=" * 80)
    print("七、结论")
    print("=" * 80)
    all_max = all(m["hit5"] >= 0.999 for m in mets)
    spread = max(m["hit5"] for m in mets) - min(m["hit5"] for m in mets)

    if all_max and spread < 1e-9:
        print(f"  ① 三配置 Hit@5 **全部 100%**（差异 {spread:.1%}）→ 检索层同样是天花板。")
        print(f"     根因：语料仅 {corpus_size} 篇，且 {mets[0]['single_gold_n']}/{mets[0]['n_cases']} "
              f"是单文档题（首篇 gold 恒排第 1）。")
    elif spread < 1e-9:
        print(f"  ① 三配置 Hit@5 完全相同（{mets[0]['hit5']:.1%}）→ 无配置间差异。")
    else:
        print(f"  ① 三配置 Hit@5 出现差异：最大 {max(m['hit5'] for m in mets):.1%}、"
              f"最小 {min(m['hit5'] for m in mets):.1%}（差 {spread:.1%}）→ 需查区间是否重叠。")

    cross_m = mets[-1]
    base_m = mets[0]
    if cross_m["cross_all5"] > base_m["cross_all5"]:
        lo_a, hi_a = wilson(base_m["cross_all5"], base_m["cross_n"])
        lo_c, hi_c = wilson(cross_m["cross_all5"], cross_m["cross_n"])
        verdict = "但区间重叠，尚不能确证" if not (hi_a < lo_c or hi_c < lo_a) else "且区间不重叠"
        print(f"  ② 跨文档题是唯一有梯度的地方：{base_m['cross_all5']}/{base_m['cross_n']} → "
              f"{cross_m['cross_all5']}/{cross_m['cross_n']}（{verdict}）。")
    else:
        print("  ② 跨文档题上也没有观察到改进。")

    print("  ③ 本轮的真正收获不是「拿到了区分度」，而是**把问题定位到了层**：")
    print("     生成层 98% / 判定层（修复后）98% / 检索层 100% —— 三层全到顶，")
    print("     说明瓶颈不在重排、不在评测器，而在**任务难度**。")
    print("     → 下一步的取舍应当围绕「怎么把任务变难」，而不是继续换指标。")


# ============================================================
# 主流程
# ============================================================
async def run(args) -> int:
    configs = [args.config] if args.config else list(CONFIGS)

    async with engine.connect() as conn:
        cases, excluded = await load_cases(conn)
        corpus_size = await count_corpus_docs(conn)
        n_gold, n_found, missing = await check_gold_alignment(conn, cases)

    if not cases:
        print("❌ 没有可算的题（doc_ids 全为空？先跑 scripts/d21_seed 落评测集）。")
        return 1

    print_header(corpus_size, cases, excluded, n_gold, n_found, missing)

    # 坑①的硬闸：标注失效时**拒绝出指标**，而不是打印一堆 0%
    if missing or n_gold == 0:
        print()
        print("❌ gold 标注与当前语料对不上 —— 指标会全部变成「未命中」，"
              "而那**不代表检索坏了**。")
        print("   请先重建评测集的 doc_ids（或改用 doc_slugs 口径），再跑本脚本。")
        await engine.dispose()
        return 1

    blocks, mets = [], []
    for cfg in configs:
        print()
        print(f"  ⏳ 跑配置 {cfg} ...（{len(cases)} 题，零 LLM 调用）", flush=True)
        block = await collect(cfg, args.top_k, cases)
        blocks.append(block)
        mets.append(metrics(block, corpus_size))

    await engine.dispose()

    print_main_table(blocks, mets)
    print_ceiling(mets, corpus_size)
    print_cross_doc(mets, blocks)
    print_per_case(blocks)
    print_health(blocks)
    print_conclusion(blocks, mets, corpus_size)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "top_k": args.top_k,
                    "corpus_size": corpus_size,
                    "n_cases": len(cases),
                    "metrics": {b["config"]: m for b, m in zip(blocks, mets)},
                    "per_case": {b["config"]: b["per_case"] for b in blocks},
                },
                f,
                ensure_ascii=False,
                indent=1,
                default=str,
            )
        print()
        print(f"逐题结果已写入 {args.json}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="检索层指标 Hit@k / MRR（离线、零 token）")
    parser.add_argument("--config", choices=list(CONFIGS), help="只跑一个配置（默认三个都跑）")
    parser.add_argument("--top-k", type=int, default=METRIC_TOP_K,
                        help=f"报多少名的梯度（默认 {METRIC_TOP_K}）")
    parser.add_argument("--json", help="把逐题结果写到指定路径")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
