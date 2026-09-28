"""
D17 验证脚本：RRF 混合检索
===========================
跑法：

    cd <项目根> && ~/.local/bin/uv run python -m scripts.d17_hybrid_verify

六段：
  A RRF 纯函数    —— 不连数据库，逐条验证公式（讲解里算过的每个数字都在这里被复算）
  B 真实两路融合  —— 融合的**身份唯一性 / 分数可重算 / 不丢任一路第一名**
  C 抽样对比      —— PRD 的验收点「优于单路（抽样）」：能证明什么、不能证明什么都如实写
  D 边界          —— 关键词路为空时的降级、top_k、无命中
  E 契约          —— 返回字段集合、类型、非空、确定性

⚠️ 断言设计遵循铁律 9：
   每条都带**正向条件**（非空、> 0），防止"空集合恒满足"式的假通过。
⚠️ 结论文字遵循铁律 11：
   一律写成 if/else 由数据决定，不硬编码在 print 里。
"""

import asyncio

from app.core.db import engine
from app.services.document_service import (
    count_documents_by_prefix,
    delete_documents_by_prefix,
    ingest_texts,
)
from app.services.retrieval_service import (
    HYBRID_CANDIDATE_K,
    RRF_K,
    fuse_rrf,
    hybrid_search,
    search,
    search_keywords,
)

VERIFY_PREFIX = "d17-hybrid-verify"

# 四份语料：刻意分成"字面派"和"语义派"，让两路的命中集合真的不一样
VERIFY_DOCS = {
    f"{VERIFY_PREFIX}-hr": (
        "员工年假天数按工龄计算：满一年 5 天，满三年 10 天，满五年 15 天，"
        "申请需提前三个工作日提交。"
    ),
    f"{VERIFY_PREFIX}-fin": (
        "报销流程：先填写报销单，再经部门经理审批，最后财务打款，整个周期约五个工作日。"
    ),
    f"{VERIFY_PREFIX}-ops": (
        "代码上线前必须在灰度环境验证；出现故障需在十分钟内完成回滚，并同步通知值班同学。"
    ),
    f"{VERIFY_PREFIX}-policy": (
        "关于调休与年假的补充说明：调休需提前一天申请，年假不可跨年累积，"
        "未休年假按日折算工资。"
    ),
}


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            self.failed += 1
            print(f"  [FAIL] {label}  {detail}")
        return ok

    def summary(self) -> None:
        total = self.passed + self.failed
        print("=" * 74)
        print(f"结果：{self.passed}/{total} 通过" + (f"，{self.failed} 失败" if self.failed else ""))
        print("=" * 74)


def _hit(chunk_id: str, **extra) -> dict:
    """造一条假的检索结果（只给融合函数用，不连数据库）。"""
    base = {"chunk_id": chunk_id, "content": f"content-{chunk_id}", "source": "s", "page_ref": None}
    base.update(extra)
    return base


def _brief(names: list[str]) -> list[str]:
    return [n.replace(f"{VERIFY_PREFIX}-", "") for n in names]


# ============================================================
# A. RRF 纯函数（不连数据库）
# ============================================================
def section_a(ck: Checker) -> None:
    print("=" * 74)
    print("A. RRF 纯函数（讲解里算过的每个数字在这里被复算）")
    print("=" * 74)

    # 构造两路列表，名次按讲解里定的位置摆：
    #   向量路：A=1、R=4、B=5、P=8、Q=10、S=30
    #   BM25 路：B=5、Q=10、S=30（A/P/R 缺席）
    v_ids = [f"v{i}" for i in range(1, 31)]
    v_ids[0], v_ids[3], v_ids[4] = "A", "R", "B"
    v_ids[7], v_ids[9], v_ids[29] = "P", "Q", "S"
    k_ids = [f"k{i}" for i in range(1, 31)]
    k_ids[4], k_ids[9], k_ids[29] = "B", "Q", "S"

    # 故意把 A 的原始相似度给到最高（0.99），B 给到很低（0.10）——
    # 如果融合偷看了分数，A 会赢；只看名次则是 B 赢。这条是"分数被扔掉"的判决性证据。
    vector_hits = [
        _hit(cid, similarity=0.99 if cid == "A" else (0.10 if cid == "B" else 0.5))
        for cid in v_ids
    ]
    keyword_hits = [_hit(cid, score=9.9 if cid == "B" else 1.0) for cid in k_ids]

    fused = fuse_rrf(vector_hits, keyword_hits, top_k=60)
    named = ["A", "B", "P", "Q", "R", "S"]
    order = [e["chunk_id"] for e in fused if e["chunk_id"] in named]
    print(f"  两路各 30 条，融合后共 {len(fused)} 条")
    print(f"  关注的 6 条排序：{order}")
    expect_order = ["B", "Q", "S", "A", "R", "P"]
    ck.check(order == expect_order, "A1 排序 = B > Q > S > A > R > P", f"实际 {order}")

    # 逐条复算分数：1/(k+rank_v) + 1/(k+rank_k)
    def expect_score(v_rank, k_rank):
        total = 0.0
        if v_rank:
            total += 1.0 / (RRF_K + v_rank)
        if k_rank:
            total += 1.0 / (RRF_K + k_rank)
        return round(total, 6)

    by_id = {e["chunk_id"]: e for e in fused}
    print("\n  逐条复算（手算 vs 代码）：")
    bad = []
    for cid in named:
        e = by_id[cid]
        exp = expect_score(e["vector_rank"], e["bm25_rank"])
        mark = "" if abs(exp - e["rrf_score"]) < 1e-9 else "  ← 不一致"
        if mark:
            bad.append(cid)
        print(f"    {cid}: 向量rank={e['vector_rank']} BM25rank={e['bm25_rank']} "
              f"→ {e['rrf_score']} (手算 {exp}){mark}")
    ck.check(not bad, "A2 每条融合分 = 两路 1/(k+rank) 之和（逐条复算一致）", f"不一致 {bad}")

    # 关键比值（讲解里算过的三个）
    ratios = {
        "B/A": by_id["B"]["rrf_score"] / by_id["A"]["rrf_score"],
        "Q/P": by_id["Q"]["rrf_score"] / by_id["P"]["rrf_score"],
        "S/R": by_id["S"]["rrf_score"] / by_id["R"]["rrf_score"],
    }
    print("\n  关键比值：")
    for name, val in ratios.items():
        print(f"    {name} = {val:.4f}")
    ck.check(abs(ratios["B/A"] - 1.877) < 0.01, "A3a 两路都 rank5 胜过单路 rank1（1.877 倍）")
    ck.check(abs(ratios["Q/P"] - 1.943) < 0.01, "A3b 两路都 rank10 胜过单路 rank8（1.943 倍）")
    ck.check(abs(ratios["S/R"] - 1.422) < 0.01, "A3c 两路都 rank30 胜过单路 rank4（1.422 倍）")

    # 判决性证据：B 的原始分（相似度 0.10、BM25 9.9 但名次第 5）vs A（相似度 0.99）
    print(f"\n  原始分对照：A similarity={by_id['A']['similarity']} → 总分 {by_id['A']['rrf_score']}")
    print(f"              B similarity={by_id['B']['similarity']} → 总分 {by_id['B']['rrf_score']}")
    ck.check(by_id["B"]["rrf_score"] > by_id["A"]["rrf_score"],
             "A4 原始相似度最高的 A 被 B 反超（证明排序只看名次、不看分数）")

    # k 的影响：k 越大名次越被压平
    print(f"\n  k 对头部集中的影响（rank1 的贡献 ÷ rank20 的贡献）：")
    k_rows = []
    for k in (1, 60, 600):
        r = (1.0 / (k + 1)) / (1.0 / (k + 20))
        k_rows.append((k, r))
        print(f"    k={k:<4} → {r:.3f}×")
    ck.check(k_rows[0][1] > k_rows[1][1] > k_rows[2][1],
             "A5 k 越大名次越被压平（k=1 时 10.5×，k=600 时 1.03×）")

    # 缺 chunk_id 的结果要被跳过，而不是让整次检索崩掉
    dirty = [_hit("ok-1"), {"content": "没有身份的片段"}, _hit("ok-2")]
    fused_dirty = fuse_rrf(dirty, [], top_k=10)
    ck.check([e["chunk_id"] for e in fused_dirty] == ["ok-1", "ok-2"],
             "A6 缺 chunk_id 的结果被跳过、不影响其余结果")

    # 单路输入 → 保序（融合在没有第二路时不能打乱原有顺序）
    only_vec = fuse_rrf(vector_hits[:5], [], top_k=5)
    ck.check([e["chunk_id"] for e in only_vec] == v_ids[:5],
             "A7 只有一路输入时，融合结果与该路顺序完全一致（保序）")

    # 空输入 → 空输出（不炸）
    ck.check(fuse_rrf([], [], top_k=5) == [], "A8 两路全空 → 返回空列表")

    # 平局之一：各自单路 rank1（分数都是 1/61）→ 有向量名次的那条优先（策略档 ④）
    tie1 = fuse_rrf([_hit("zzz")], [_hit("aaa")], top_k=5)
    tie2 = fuse_rrf([_hit("zzz")], [_hit("aaa")], top_k=5)
    print(f"\n  平局①（各单路 rank1，同为 1/61）：{[e['chunk_id'] for e in tie1]}")
    ck.check(
        [e["chunk_id"] for e in tie1] == [e["chunk_id"] for e in tie2] == ["zzz", "aaa"],
        "A9 单路平局时向量路条目优先，且两次结果一致（策略确定，非随机）",
    )

    # 平局之二（**结构性平局，实测真出现过**）：两条文档在两路里名次交换
    #   X = 向量1 / BM25 2，Y = 向量2 / BM25 1 → 都是 1/61 + 1/62，**精确相等**
    swap = fuse_rrf([_hit("X"), _hit("Y")], [_hit("Y"), _hit("X")], top_k=5)
    print(f"  平局②（名次交换，X=1/2、Y=2/1）：{[e['chunk_id'] for e in swap]} "
          f"分数 {[e['rrf_score'] for e in swap]}")
    ck.check(
        swap[0]["rrf_score"] == swap[1]["rrf_score"] and [e["chunk_id"] for e in swap] == ["X", "Y"],
        "A9b 名次交换型平局分数精确相等，按「向量名次优先」策略决出先后（X 赢）",
    )

    # 结构性性质（可证明，不是观察）：**两路都命中的条目，必胜"唯一名次比它更差"的单路条目。**
    #   证明：a 在内容路的名次 r1 < b 的唯一名次 r
    #   → score(a) ≥ 1/(k+r1) + 1/(k+r2) > 1/(k+r1) > 1/(k+r) = score(b)
    #   推论（强得多）：只要交集非空且交集中最大名次 ≤ k，则 2/(k+m) ≥ 2/(2k) = 1/k > 1/(k+1)
    #   → **融合第一名必定来自交集** —— 单路第一名（哪怕是向量 rank1）也抢不走。
    violations = []
    for a in fused:
        if not (a["vector_rank"] and a["bm25_rank"]):
            continue
        best_a = min(a["vector_rank"], a["bm25_rank"])
        for b in fused:
            if b is a:
                continue
            only = [r for r in (b["vector_rank"], b["bm25_rank"]) if r is not None]
            if len(only) == 1 and only[0] > best_a and b["rrf_score"] >= a["rrf_score"]:
                violations.append((a["chunk_id"], b["chunk_id"]))
    ck.check(not violations,
             "A10 交集条目必胜「唯一名次更差」的单路条目（结构性性质，无违例）",
             f"违例 {violations}")

    # 行列式验证：交集非空时，融合首位必须 ∈ 交集
    top1 = fused[0]
    ck.check(top1["vector_rank"] is not None and top1["bm25_rank"] is not None,
             f"A11 交集非空时融合首位来自交集（实际 {top1['chunk_id']}）")
    print()


# ============================================================
# B. 真实两路融合（连库）
# ============================================================
async def section_b(ck: Checker, query: str) -> None:
    print("=" * 74)
    print(f"B. 真实两路融合（query={query!r}）")
    print("=" * 74)

    vec = await search(query, top_k=HYBRID_CANDIDATE_K)
    kw = await search_keywords(query, top_k=HYBRID_CANDIDATE_K)
    v_ids = [h["chunk_id"] for h in vec]
    k_ids = [h["chunk_id"] for h in kw]
    union = set(v_ids) | set(k_ids)
    inter = set(v_ids) & set(k_ids)

    print(f"  向量路 {len(vec)} 条 / BM25 路 {len(kw)} 条 / 交集 {len(inter)} 条 / 并集 {len(union)} 条")

    # 融合时 top_k 给足（并集大小），先验证"融合是全集上的正确排序"，再验证 top_k 截断
    full = fuse_rrf(vec, kw, top_k=len(union))
    full_ids = [e["chunk_id"] for e in full]
    print(f"  融合后 {len(full)} 条")

    ck.check(len(union) > 0 and len(full) == len(union),
             f"B1 融合条数 == 两路并集条数（{len(full)}/{len(union)}）",
             "融合出现丢条或重复")
    ck.check(len(full_ids) == len(set(full_ids)),
             "B2 融合结果里 chunk_id 无重复（同一片被两路命中时是合并、不是并列）")
    ck.check(set(full_ids) == union,
             "B3 融合结果是两路的并集（既没有丢任一路的条目，也没有凭空多出）")

    # 分数可重算：拿结果里记录的 rank 反推分数，必须与 rrf_score 一致
    mismatch = []
    for e in full:
        exp = 0.0
        if e["vector_rank"]:
            exp += 1.0 / (RRF_K + e["vector_rank"])
        if e["bm25_rank"]:
            exp += 1.0 / (RRF_K + e["bm25_rank"])
        if abs(round(exp, 6) - e["rrf_score"]) > 1e-9:
            mismatch.append((e["chunk_id"], e["rrf_score"], round(exp, 6)))
    ck.check(not mismatch, "B4 每条融合分可由记录的 rank 反推得到（分数链路自洽）", f"{mismatch}")

    # 两路各自的第一名都必须还在（融合不能把任何一路的头部丢掉）
    top1_kept = []
    if v_ids:
        top1_kept.append(v_ids[0] in set(full_ids))
    if k_ids:
        top1_kept.append(k_ids[0] in set(full_ids))
    ck.check(bool(top1_kept) and all(top1_kept),
             "B5 两路各自的第一名都出现在融合结果里")

    # 降序
    scores = [e["rrf_score"] for e in full]
    ck.check(scores == sorted(scores, reverse=True), "B6 融合结果按 rrf_score 降序")

    # rank 字段能正确表达"来自哪一路"
    inconsistent = [
        e["chunk_id"] for e in full
        if (e["chunk_id"] in inter) != (e["vector_rank"] is not None and e["bm25_rank"] is not None)
    ]
    ck.check(not inconsistent,
             "B7 交集的条目两侧 rank 都非空；单路独有条目另一侧为 None",
             f"不一致 {inconsistent}")

    print("\n  融合结果明细（前后各 5 条）：")
    for e in full[:5] + (full[-5:] if len(full) > 5 else []):
        name = (e["source"] or "").replace(f"{VERIFY_PREFIX}-", "")
        print(f"    {name:<10} rrf={e['rrf_score']:.6f} "
              f"向量rank={e['vector_rank']} bm25rank={e['bm25_rank']}")
    print()


# ============================================================
# C. 抽样对比（PRD 验收点：优于单路）
# ============================================================
async def section_c(ck: Checker) -> None:
    print("=" * 74)
    print("C. 抽样对比：融合 vs 单路（PRD 验收点「优于单路（抽样）」）")
    print("=" * 74)

    queries = ["灰度回滚", "年假有几天", "报销要谁审批"]
    show_k = 5      # 打印用的窗口（好看）
    top_k = 5
    covered_all = True
    top1_in_intersection = True
    fused_top1_same_as_vector = 0
    fused_top1_same_as_bm25 = 0

    for q in queries:
        vec = await search(q, top_k=top_k)
        kw = await search_keywords(q, top_k=top_k)
        fused = await hybrid_search(q, top_k=show_k)
        # ⚠ 覆盖性断言必须用**足够大的窗口**：库里还躺着 d16/探针留下的外部语料，
        #   它们也会被两路命中、正当地占掉 top5 名额（这就是第一次跑 C1 红掉的原因 ——
        #   是断言把窗口开小了，不是融合丢条；融合"不丢条"的结构性保证已在 B3 证明）。
        wide = await hybrid_search(q, top_k=HYBRID_CANDIDATE_K)
        v_own = [h["source"] for h in vec if h["source"].startswith(VERIFY_PREFIX)]
        k_own = [h["source"] for h in kw if h["source"].startswith(VERIFY_PREFIX)]
        f_own = [h["source"] for h in fused if h["source"].startswith(VERIFY_PREFIX)]
        f_own_wide = {h["source"] for h in wide if h["source"].startswith(VERIFY_PREFIX)}

        print(f"\n  查询 {q!r}")
        print(f"    向量 top{show_k} : {_brief(v_own)}")
        print(f"    BM25 top{show_k} : {_brief(k_own)}")
        print(f"    融合 top{show_k} : {_brief(f_own)}")

        # C1：窗口足够（20）时，融合必须覆盖两路各自的前 3 名（前 3 名是"路"的头部结论）
        need = set(v_own[:3]) | set(k_own[:3])
        if need and not need.issubset(f_own_wide):
            covered_all = False
            print(f"      ⚠ 窗口 {HYBRID_CANDIDATE_K} 下仍未覆盖：{sorted(need - f_own_wide)}")

        # C1b：只要两路有交集，融合首位必须来自交集（A10 那条性质在真实数据上的验证）
        if wide:
            head = wide[0]
            if head["vector_rank"] is None or head["bm25_rank"] is None:
                top1_in_intersection = False
                print(f"      ⚠ 融合首位 {head['source']} 不在交集里")

        if f_own and v_own:
            fused_top1_same_as_vector += f_own[0] == v_own[0]
        if f_own and k_own:
            fused_top1_same_as_bm25 += f_own[0] == k_own[0]

    ck.check(covered_all,
             f"C1 窗口 {HYBRID_CANDIDATE_K} 时融合覆盖两路各自前三名（不丢任一路头部）")
    ck.check(top1_in_intersection,
             "C1b 两路有交集时，融合首位来自交集（单路第一名抢不走）")

    # 结论由数据决定（铁律 11）——不预设"融合一定更好"
    n = len(queries)
    print(f"\n  top1 一致性（共 {n} 个查询）：")
    print(f"    融合 top1 == 向量 top1：{fused_top1_same_as_vector}/{n}")
    print(f"    融合 top1 == BM25 top1：{fused_top1_same_as_bm25}/{n}")
    if fused_top1_same_as_vector == n:
        print("  → 本语料上融合**没有改变**任何一个查询的首位结果：")
        print("     向量路自己就把正确答案排第一，两路高度一致，RRF 的'共识仲裁'无从发挥。")
    else:
        print("  → 本语料上融合**改变了**部分查询的首位结果（两路分歧处 RRF 做出了仲裁）。")

    # 「优于单路」到底能证明多少 —— 诚实交代
    ck.check(True, "C2 抽样对比已记录（首位是否改变按数据如实输出，不作预设断言）")
    print("\n  对 PRD 验收点的如实交代：")
    print("  ① 能证明的：融合的**召回覆盖面 ≥ 任一路**（并集），且不丢任一路头部 —— 这是结构性保证；")
    print("  ② 不能证明的：'融合的**排序质量**更高'。质量要靠标注过的评测集打分（D20-D24），")
    print("     本脚本的 mini 语料只有 4 篇、两路高度一致，排序差异几乎不出现。")
    print("     PRD 把验收点写成「优于单路（**抽样**）」，正是因为它此时只能抽样看现象，")
    print("     严格结论留给消融实验（D23）。")
    print()


# ============================================================
# D. 边界
# ============================================================
async def section_d(ck: Checker) -> None:
    print("=" * 74)
    print("D. 边界")
    print("=" * 74)

    # D1 关键词路为空 → 融合自动退化成向量单路，且**保序**
    weird = "。。。！！！"
    vec = await search(weird, top_k=5)
    kw = await search_keywords(weird, top_k=5)
    fused = await hybrid_search(weird, top_k=5)
    print(f"  退化查询 {weird!r}：向量 {len(vec)} 条 / BM25 {len(kw)} 条 / 融合 {len(fused)} 条")
    ck.check(kw == [], "D1a 纯标点查询在关键词路上被清洗为空（返回 []，不报错）")
    ck.check([h["chunk_id"] for h in fused] == [h["chunk_id"] for h in vec],
             "D1b BM25 路为空时，融合结果与向量路顺序完全一致（降级而非打乱）")
    ck.check(all(e["bm25_rank"] is None for e in fused),
             "D1c 降级时所有条目的 bm25_rank 均为 None（来源可追溯）")

    # D2 top_k 截断
    one = await hybrid_search("灰度回滚", top_k=1)
    ck.check(len(one) == 1, f"D2 top_k=1 生效（实际 {len(one)} 条）")

    # D3 库里根本没有的词：向量路仍会给"最近邻"（这是向量的性质，不是 bug）
    none_hit = await hybrid_search("量子纠缠拓扑绝缘体", top_k=3)
    print(f"\n  生僻词查询：融合 {len(none_hit)} 条（向量路总会返回最近邻，BM25 路为 0）")
    ck.check(all(e["bm25_rank"] is None for e in none_hit),
             "D3 生僻词：BM25 无命中、向量给最近邻，融合不报错且标清来源")
    print()


# ============================================================
# E. 契约与确定性
# ============================================================
async def section_e(ck: Checker) -> None:
    print("=" * 74)
    print("E. 返回契约与确定性")
    print("=" * 74)

    query = "灰度回滚"
    fused = await hybrid_search(query, top_k=5)
    ck.check(len(fused) > 0, "E1a 融合返回非空（正向条件）")

    expect_keys = {
        "chunk_id", "content", "source", "page_ref",
        "rrf_score", "vector_rank", "bm25_rank",
        "similarity", "bm25_score", "retriever",
    }
    keys = set(fused[0].keys())
    print(f"  返回字段：{sorted(keys)}")
    ck.check(keys == expect_keys, "E1b 返回字段与约定完全一致", f"实际 {keys}")
    ck.check(all(isinstance(e["chunk_id"], str) and e["chunk_id"] for e in fused),
             "E1c 每条的 chunk_id 都是非空字符串")
    ck.check(all(e["rrf_score"] > 0 for e in fused),
             "E1d 融合分全部为正（1/(k+rank) 恒 > 0）")
    ck.check(all(e["retriever"] == "hybrid" for e in fused),
             "E1e retriever 标为 hybrid（与单路的 vector/bm25 可区分）")

    # 确定性：同一查询两次结果必须完全一致（否则"两次运行结果不一致"要按铁律 9 排查）
    again = await hybrid_search(query, top_k=5)
    ck.check([e["chunk_id"] for e in fused] == [e["chunk_id"] for e in again],
             "E2 同一查询两次调用，融合顺序完全一致（确定性）")

    # 融合分与并列关系：必须严格不增
    scores = [e["rrf_score"] for e in fused]
    ck.check(all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)),
             "E3 融合分严格不增（含并列）")
    print()


async def main() -> None:
    ck = Checker()

    # A 段不连库，先跑（纯函数出问题就没必要连数据库了）
    section_a(ck)

    # 清掉上次残留的同名测试文档，保证幂等
    # 2026-09-28 改：统一走 delete_documents_by_prefix（原先手写 SQL + 逐条删），
    # 顺带拿到 LIKE 通配符转义 —— 前缀里出现下划线时手写 SQL 会误伤别的文档。
    stale = await delete_documents_by_prefix(VERIFY_PREFIX)
    if stale:
        print(f"（已清理上次残留的 {stale} 份测试文档）\n")

    # 造语料（走真实写路径，顺带验证 content_tokens 在入库时被算出来）
    for filename, content in VERIFY_DOCS.items():
        doc_id, count = await ingest_texts([content], filename=filename)
        print(f"入库 {filename:28} 切片 {count} 个")
    print()

    query = "灰度回滚"
    await section_b(ck, query)
    await section_c(ck)
    await section_d(ck)
    await section_e(ck)

    # 收尾：删掉本脚本造的语料（避免污染后续 BM25 的语料统计量 N/df/avgdl）
    # + 断言真的收干净了（2026-09-28 补：原先只删不断言，"删了 0 份"也会打印
    #   "已清理"，属于"做了但没人验证做过"）
    removed = await delete_documents_by_prefix(VERIFY_PREFIX)
    left = await count_documents_by_prefix(VERIFY_PREFIX)
    print(f"（已清理本脚本造的 {removed} 份测试文档）")
    ck.check(left == 0, f"Z1 本脚本语料已收干净（残留 {left}）")

    await engine.dispose()
    ck.summary()


if __name__ == "__main__":
    asyncio.run(main())
