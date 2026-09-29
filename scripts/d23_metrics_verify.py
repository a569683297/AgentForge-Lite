"""
D23 检索层指标的验证脚本
============================
为什么指标也需要验证脚本：
    指标算错了**不会报错、不会崩**，只会让数字悄悄偏 —— 与 D13/D16/D18 三次
    「结论与数据矛盾却照念」是同一族。所以关键性质必须写成 `assert`。

本脚本分五段：
    A 纯函数：Wilson 置信区间
    B 纯函数：文档级去重（ranks_by_document）—— 最容易写错、错了不报错的一步
    C 纯函数：metrics() 手算对照（三题人工构造，每个指标都手算过）
    D 回库：分母口径 + gold 对齐核验（含核验本身的负对照）
    E 负对照：真跑一次检索，把 gold 换成**错的文档** → 指标必须掉下来
              （回答「什么情况下这个指标会假通过」）

A/B/C 段不碰数据库，秒级；D/E 段各查一次库，E 段跑 35 次纯向量检索（约 3 秒）。
**全程零 LLM 调用。**

用法（项目根目录）：
    uv run python -m scripts.d23_metrics_verify
"""

import asyncio
import uuid

import app.models  # noqa: F401
from sqlalchemy import text

from app.core.db import engine
from scripts.d23_retrieval_metrics import (
    check_gold_alignment,
    collect,
    count_corpus_docs,
    load_cases,
    metrics,
    ranks_by_document,
    wilson,
)

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """一条断言。输出格式与 d22_verify 保持一致（便于统一统计）。"""
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"{name:<5} [PASS] {detail}")
    else:
        _failed += 1
        print(f"{name:<5} [FAIL] {detail}")


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) < tol


# ============================================================
# A 段：Wilson 置信区间
# ============================================================
def segment_a() -> None:
    print()
    print("=" * 78)
    print("A 段：Wilson 置信区间（纯函数）")
    print("=" * 78)

    lo, hi = wilson(10, 10)
    check("A1", close(lo, 0.72246, 1e-4) and close(hi, 1.0, 1e-9),
          f"10/10 → [{lo:.4f}, {hi:.4f}]（上界必须恰好 1.0，不能超过 1）")
    check("A2", lo < 1.0, f"10/10 的下界必须严格小于 1（否则等于宣称样本无限大）")

    lo0, hi0 = wilson(0, 10)
    check("A3", close(lo0, 0.0, 1e-9) and close(hi0, 0.27753, 1e-4),
          f"0/10 → [{lo0:.4f}, {hi0:.4f}]（下界恰好 0，上界 < 0.3）")

    lo5, hi5 = wilson(5, 10)
    check("A4", close(lo5 + hi5, 1.0, 1e-6),
          f"5/10 → [{lo5:.4f}, {hi5:.4f}]（p=0.5 时必须对称）")

    loe, hie = wilson(0, 0)
    check("A5", (loe, hie) == (0.0, 1.0), f"n=0 → {loe, hie}（分母为 0 时给全区间，不给假精确）")

    bad = [k for k in range(0, 11) if not (0.0 <= wilson(k, 10)[0] <= k / 10 <= wilson(k, 10)[1] <= 1.0)]
    check("A6", not bad, f"遍历 0/10~10/10：下界<=点估计<=上界且都在 [0,1] 内（越界的：{bad}）")


# ============================================================
# B 段：文档级去重
# ============================================================
def segment_b() -> None:
    print()
    print("=" * 78)
    print("B 段：文档级去重（纯函数）—— 最容易写错、错了不报错的一步")
    print("=" * 78)

    hits = [{"chunk_id": "1"}, {"chunk_id": "2"}, {"chunk_id": "3"}, {"chunk_id": "4"}]
    c2d = {"1": "D1", "2": "D1", "3": "D1", "4": "D2"}

    got = ranks_by_document(hits, c2d)
    check("B1", got == {"D1": 1, "D2": 4}, f"三片同文档 → {got}（D1 只记最早的第 1 名）")
    check("B2", len(got) == 2, f"4 条片段压成 {len(got)} 篇文档（不是 3 篇）")
    check("B3", got.get("D1") == 1, "D1 的名次是 1 而不是 3 —— 用 setdefault 取最早，不是最后一次")

    check("B4", ranks_by_document([], c2d) == {}, "空输入 → 空结果（不抛错）")
    check("B5", ranks_by_document([{"chunk_id": "999"}], c2d) == {},
          "映射里没有的 chunk_id → 跳过（宁可少一条，也不要因脏数据整轮崩）")

    # 反面对照：证明"正确写法"不是多余的
    naive: dict = {}
    for rank, h in enumerate(hits, start=1):
        did = c2d.get(h["chunk_id"])
        if did:
            naive[did] = rank          # ← 直接赋值：留下的是最后一次出现的名次
    check("B6", naive["D1"] == 3 and got["D1"] == 1,
          f"对照：直接赋值会得到 D1={naive['D1']}（错），setdefault 得到 {got['D1']}（对）"
          " —— 名次被系统性拉低且不报错，这就是必须有断言的原因")


# ============================================================
# C 段：metrics() 手算对照
# ============================================================
def segment_c() -> None:
    print()
    print("=" * 78)
    print("C 段：metrics() 手算对照（三题人工构造）")
    print("=" * 78)

    # 人工构造（corpus_size=8）：
    #   X1 单文档题，gold 第 1 名；2 篇非 gold 在第 2、3 名
    #   X2 单文档题，**未命中**；2 篇非 gold 在第 1、2 名
    #   X3 跨文档题，gold 在第 1、4 名；2 篇非 gold 在第 2、3 名
    per_case = [
        {"case_key": "X1", "category": "doc_qa", "difficulty": "easy", "n_gold": 1,
         "gold_ranks": [1], "non_gold_ranks": [2, 3], "n_hits": 5, "distinct_docs": 3,
         "ranks_by_doc": {}},
        {"case_key": "X2", "category": "doc_qa", "difficulty": "easy", "n_gold": 1,
         "gold_ranks": [], "non_gold_ranks": [1, 2], "n_hits": 5, "distinct_docs": 3,
         "ranks_by_doc": {}},
        {"case_key": "X3", "category": "cross_doc", "difficulty": "medium", "n_gold": 2,
         "gold_ranks": [1, 4], "non_gold_ranks": [2, 3], "n_hits": 5, "distinct_docs": 4,
         "ranks_by_doc": {}},
    ]
    m = metrics({"per_case": per_case}, corpus_size=8)

    check("C1", m["n_cases"] == 3, f"题数 {m['n_cases']}")
    check("C2", close(m["hit1"], 2 / 3), f"Hit@1 = {m['hit1']:.4f}（期望 2/3：X1 与 X3 命中）")
    check("C3", close(m["hit5"], 2 / 3), f"Hit@5 = {m['hit5']:.4f}（期望 2/3：X2 未命中）")
    check("C4", close(m["hit_all5"], 2 / 3),
          f"Hit@5(all) = {m['hit_all5']:.4f}（期望 2/3：X1 单篇命中、X3 两篇都在 top5）")
    check("C5", close(m["mrr"], (1 + 0 + 1) / 3),
          f"MRR = {m['mrr']:.4f}（期望 {(1+0+1)/3:.4f}：X2 未命中贡献 0）")
    check("C6", close(m["mrr_last"], (1 + 0 + 0.25) / 3),
          f"MRR_last = {m['mrr_last']:.4f}（期望 {(1+0+0.25)/3:.4f}：X3 取**最后一篇**的第 4 名）")
    check("C7", close(m["mrr_last"], m["mrr"]) is False,
          f"MRR_last({m['mrr_last']:.4f}) ≠ MRR({m['mrr']:.4f}) —— "
          "这正是要引入它的原因：标准 MRR 在跨文档题上看不出第二篇的退化")
    check("C8", m["first_rank_one"] == 2, f"首篇排第 1 的题：{m['first_rank_one']}/3")
    check("C9", m["single_gold_n"] == 2, f"只有 1 篇 gold 的题：{m['single_gold_n']}/3")
    check("C10", m["cross_n"] == 1 and m["cross_all5"] == 1,
          f"跨文档题 {m['cross_n']} 条、两篇都进 top5 的 {m['cross_all5']} 条")

    exp_rand = (2 / 7 + 2 / 7 + 2 / 6) / 3
    check("C11", close(m["rand5"], exp_rand, 1e-6),
          f"随机基线@5 = {m['rand5']:.4f}（期望 {exp_rand:.4f} = (2/7+2/7+2/6)/3）")


# ============================================================
# D 段：分母口径 + gold 对齐（回库）
# ============================================================
async def segment_d(conn) -> list[dict]:
    print()
    print("=" * 78)
    print("D 段：分母口径 + gold 对齐核验（回库核对，不靠记忆）")
    print("=" * 78)

    cases, excluded = await load_cases(conn)
    corpus_size = await count_corpus_docs(conn)
    n_gold, n_found, missing = await check_gold_alignment(conn, cases)

    check("D1", len(cases) == 35, f"参与指标的题数 {len(cases)}（期望 35）")
    check("D2", len(excluded) == 15, f"被排除的题数 {len(excluded)}（期望 15）")
    neg = sum(1 for e in excluded if "负例" in e["reason"])
    tool = sum(1 for e in excluded if "工具类题" in e["reason"])
    check("D3", neg == 5 and tool == 10, f"排除构成：负例 {neg} + 工具类题 {tool}")
    single = sum(1 for c in cases if len(c["doc_ids"]) == 1)
    cross = sum(1 for c in cases if len(c["doc_ids"]) >= 2)
    check("D4", single == 25 and cross == 10, f"单文档 {single} + 跨文档 {cross}")
    check("D5", corpus_size == 8, f"语料文档数 {corpus_size}（检索任务的难度上限）")
    check("D6", n_gold == n_found and n_gold > 0,
          f"gold 对齐：{n_found}/{n_gold} 全部能在 documents 里找到"
          + (f"，缺失 {missing[:2]}" if missing else ""))

    # D7：核验本身的负对照 —— 喂一个不存在的 UUID，它必须报出来
    fake = [{"doc_ids": [uuid.uuid4()]}]
    _, fake_found, fake_missing = await check_gold_alignment(conn, fake)
    check("D7", fake_found == 0 and len(fake_missing) == 1,
          "负对照：喂一个不存在的 UUID → 核验报出 1 个缺失"
          "（证明它不是在恒真地打 ✅）")

    return cases


# ============================================================
# E 段：负对照 —— 指标会不会假通过
# ============================================================
async def segment_e(conn, cases: list[dict]) -> None:
    print()
    print("=" * 78)
    print("E 段：负对照 —— 把 gold 换成**错的文档**，指标必须掉下来")
    print("=" * 78)

    corpus_rows = (
        await conn.execute(text("select id from documents order by filename"))
    ).all()
    corpus_ids = [r[0] for r in corpus_rows]

    block = await collect("pure_vector", 10, cases)
    m = metrics(block, len(corpus_ids))

    check("E1", close(m["hit5"], 1.0),
          f"真 gold：Hit@5 = {m['hit5']:.1%}（这份语料上纯向量也能全中）")

    # 换错 gold：每题都换成「语料里第一篇不在本题 gold 中的文档」，保证与真答案不同
    wrong_hits = 0
    for r, c in zip(block["per_case"], cases):
        gold = {str(x) for x in c["doc_ids"]}
        wrong = next(str(d) for d in corpus_ids if str(d) not in gold)
        ranks = [rank for did, rank in r["ranks_by_doc"].items() if did == wrong]
        if any(x <= 5 for x in ranks):
            wrong_hits += 1
    rate = wrong_hits / len(block["per_case"])
    check("E2", rate < 0.4,
          f"错 gold：Hit@5 = {rate:.1%}（必须显著低于真 gold，否则说明比对逻辑恒真）")

    check("E3", m["rand5"] < 0.5,
          f"随机基线@5 = {m['rand5']:.1%}（远低于 100% 才说明 Hit@5 有信息量）")


# ============================================================
# 主流程
# ============================================================
async def main() -> int:
    segment_a()
    segment_b()
    segment_c()

    async with engine.connect() as conn:
        cases = await segment_d(conn)
        await segment_e(conn, cases)

    await engine.dispose()

    total = _passed + _failed
    print()
    print("=" * 78)
    print(f"验证结果：{_passed}/{total} 通过" + ("  ✅ 全部通过" if not _failed else f"  ❌ {_failed} 条失败"))
    print("=" * 78)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
