"""
D19 验证脚本：重排落地 + 检索配置化
=====================================
跑法：

    cd <项目根> && ~/.local/bin/uv run python -m scripts.d19_rerank_verify

八段：
  A 配置闸门      —— 不连库。三配置合法性、非法值必须**报错而不是静默回退**
  B 语料自建      —— 造 300 字真实尺寸的合成语料（生产切片尺寸），跑完即清
  C 三配置可切    —— PRD §13.2 的验收点，逐条断言三条链路的产物与字段契约
  D 重排真的生效  —— 顺序被改动了多少、分数单调性、与 similarity 不是同一个数
  E 降级可归因    —— 注入"抛异常"和"超时"两种故障，断言降级后**能认出自己降级了**
  F 上层契约      —— 工具层格式化文本里不出现 "None"，引用收集器仍拿到结构
  G 延迟          —— B vs C 的端到端耗时（必须连 load 一起报，否则数字不可解释）
  H 清理          —— 本脚本造的语料自己收干净，且不碰库里既有文档

⚠️ 断言设计遵循铁律 9：每条都带**正向条件**（非空、> 0），防止"空集合恒满足"式假通过。
⚠️ 结论文字遵循铁律 11：一律 if/else 由数据决定，绝不硬编码在 print 里。
⚠️ 延迟数字遵循铁律 11：必须连 load average 一起报，忙时只能当上界。
⚠️ 语料是本脚本**自己造的**（合成文本），不代表真实语料的语义分布 ——
   它只用于验证"链路通不通、口径对不对"，效果类结论留给 D20-D24 的评测集。
"""

import asyncio
import os
import random
import time

from pydantic import ValidationError
from sqlalchemy import text

import app.services.rerank_service as rerank_service
import app.tools.retrieval as tool_module
from app.config import RETRIEVER_CONFIGS, Settings, settings
from app.core.citation import get_sources, reset_sources
from app.core.db import async_session_factory, engine
from app.services.document_service import delete_document, ingest_texts
from app.services.rerank_service import rerank
from app.services.retrieval_service import hybrid_search, retrieve, search, search_keywords
from app.tools.retrieval import search_documents

VERIFY_PREFIX = "d19-verify"

# ------------------------------------------------------------
# 合成语料：目标 ~730 字/篇 → 恰好切成 3 片 300 字（生产尺寸）
# ------------------------------------------------------------
_ROLES = ["部门经理", "直属主管", "财务专员", "值班同学", "项目负责人"]
_SLA = ["一个工作日", "三个工作日", "五个工作日", "一周"]
_RULES = ["逾期需向分管领导报备", "特殊情况须附书面说明", "变更须留存变更记录", "未按时完成计入当月考核"]
_AMOUNTS = [1, 3, 5, 10, 20]

# 含主题关键词的句子：保证 BM25 那一路能被真正命中
_KEYWORD_TEMPLATES = [
    "关于{kw}的流程规定：由{role}受理，需在{sla}内完成，{rule}。",
    "{kw}出现异常时，须立即通知{role}并留存处理记录。",
    "{kw}的审批权限按金额分级，超出部分由{role}复核。",
    "涉及{kw}的变更须提前报备，{rule}。",
    "{kw}相关材料应归档保存，保存期不少于三年。",
    "接触{kw}之前须完成对应培训并通过考核。",
    "{kw}的执行情况每季度汇总一次，由{role}通报。",
]

# 通用句子：把篇幅撑到生产尺寸（内容分布不重要，尺寸才重要）
_GENERIC_TEMPLATES = [
    "各部门应在{sla}内完成自查，{rule}。",
    "单笔金额超过{amt}万元的支出需报分管领导审批。",
    "相关记录由{role}统一归档，保存期不少于三年。",
    "制度发布后如有修订，以最新版本为准。",
    "跨部门协作事项由{role}牵头协调，{rule}。",
    "所有操作须留痕，便于事后追溯。",
    "月度结果由{role}汇总后公示。",
    "涉及外部供应商的事项应在{sla}内完成初步评估。",
]

# 八篇语料：主题 + 每篇独有的关键词（让 BM25 的命中集合是可预期的）
TOPICS = [
    ("hr", "年假"),
    ("fin", "报销"),
    ("ops", "灰度回滚"),
    ("sec", "密钥轮换"),
    ("buy", "采购比价"),
    ("att", "考勤"),
    ("train", "入职培训"),
    ("contract", "合同评审"),
]


# 每篇拼到 ≥700 字就停。为什么上限也要卡住：
# split_text 的 step = 300-50 = 250，文档长过 750 就会多切出第 4 片、
# 且第 4 片是几十字的尾巴 —— 那会把"平均切片长度"这个指标拖下来。
# 700~750 这个区间恰好落在「3 片、且前两片满 300」上。
TARGET_DOC_CHARS = 700


def build_doc(keyword: str, seed: int) -> str:
    """
    生成一篇 700~735 字的合成制度文本（固定 seed → 每次跑完全一致）。

    ⚠ 这是**合成语料**：句子由模板 + 随机词槽拼出来，语义分布不真实。
       它只用于验证"链路通不通、口径对不对、尺寸是不是生产级"，
       **不能**用它得出任何效果类结论（那要等 D20-D24 的真实评测集）。
    """
    rng = random.Random(seed)
    sentences: list[str] = []
    length = 0
    index = 0
    while length < TARGET_DOC_CHARS:
        if index % 4 == 0:                  # 每 4 句插一句含关键词的 → 关键词约出现 8 次
            sentence = rng.choice(_KEYWORD_TEMPLATES).format(
                kw=keyword,
                role=rng.choice(_ROLES),
                sla=rng.choice(_SLA),
                rule=rng.choice(_RULES),
            )
        else:
            sentence = rng.choice(_GENERIC_TEMPLATES).format(
                role=rng.choice(_ROLES),
                sla=rng.choice(_SLA),
                rule=rng.choice(_RULES),
                amt=rng.choice(_AMOUNTS),
            )
        sentences.append(sentence)
        length += len(sentence)
        index += 1
    return "".join(sentences)


# ------------------------------------------------------------
# 断言器（与 D16/D17 同一套）
# ------------------------------------------------------------
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
        print("=" * 72)
        print(f"结果：{self.passed}/{total} 通过，{self.failed} 失败")
        print("=" * 72)


def load_tag() -> str:
    """延迟数字必须连 load 一起报，否则不可解释（铁律 11）。"""
    return f"load={os.getloadavg()[0]:.2f}/{os.cpu_count() or 1}核"


# ============================================================
# A 配置闸门（不连库）
# ============================================================
def section_a(ck: Checker) -> None:
    print("\n" + "=" * 72)
    print("A 配置闸门：三配置合法性 —— 非法值必须报错，不许静默回退")
    print("=" * 72)

    ck.check(len(RETRIEVER_CONFIGS) == 3 and set(RETRIEVER_CONFIGS) == {
        "pure_vector", "hybrid", "hybrid_rerank"
    }, "A1 三配置清单正确", f"实际={RETRIEVER_CONFIGS}")

    ck.check(settings.retriever_config in RETRIEVER_CONFIGS,
             f"A2 当前生效配置合法（{settings.retriever_config}）",
             f"实际={settings.retriever_config!r}")

    # A3：pydantic 校验器必须在**构造时**拦住非法值
    try:
        Settings(retriever_config="hybrid_rerankk")     # 故意多打一个 k
        raised = False
    except ValidationError:
        raised = True
    ck.check(raised, "A3 非法 RETRIEVER_CONFIG 在构造 Settings 时即报错（不是静默回退）")

    # A4：大小写/空白应被规范化，而不是当作非法值
    normalized_ok = False
    try:
        normalized_ok = Settings(retriever_config="  Hybrid ").retriever_config == "hybrid"
    except ValidationError:
        normalized_ok = False
    ck.check(normalized_ok, "A4 '  Hybrid ' 被规范化为 'hybrid'（不是当作非法值拒绝）")

    # A5：默认值与 PRD §9.5 消融矩阵口径一致
    ck.check(Settings().retriever_config == "hybrid_rerank",
             "A5 默认配置为 hybrid_rerank（链路最全的一档）")
    print()


# ============================================================
# C 三配置可切
# ============================================================
COMMON_KEYS = {"chunk_id", "content", "source", "page_ref", "similarity", "retriever"}


async def section_c(ck: Checker, query: str) -> dict[str, list[dict]]:
    print("\n" + "=" * 72)
    print(f"C 三配置可切（PRD §13.2 验收点）  query={query!r}")
    print("=" * 72)

    out: dict[str, list[dict]] = {}
    for name in RETRIEVER_CONFIGS:
        started = time.perf_counter()
        results = await retrieve(query, config=name)
        elapsed_ms = (time.perf_counter() - started) * 1000
        out[name] = results
        print(f"  {name:14s} 返回 {len(results)} 条  耗时 {elapsed_ms:7.1f}ms")

    for name, results in out.items():
        ck.check(len(results) > 0, f"C1[{name}] 结果非空（正向条件，防空集合假通过）")
        ck.check(len(results) == 5, f"C2[{name}] 条数 = DEFAULT_TOP_K(5)", f"实际={len(results)}")
        ck.check(COMMON_KEYS <= set(results[0].keys()),
                 f"C3[{name}] 公共键齐全",
                 f"缺={COMMON_KEYS - set(results[0].keys())}")
        ck.check(all(r.get("content") for r in results), f"C4[{name}] 每条都有 content")

    # C5：归因字段 —— 三条链路的 retriever 必须各不相同，否则消融无法分组
    labels = {name: {r["retriever"] for r in out[name]} for name in RETRIEVER_CONFIGS}
    for name in RETRIEVER_CONFIGS:
        print(f"    {name:14s} retriever 标记集合 = {sorted(labels[name])}")
    ck.check(labels["pure_vector"] == {"pure_vector"}, "C5[a] pure_vector 可归因（D17 漏了这层标记）")
    ck.check(labels["hybrid"] == {"hybrid"}, "C5[b] hybrid 可归因")
    ck.check(labels["hybrid_rerank"] == {"hybrid_rerank"},
             "C5[c] hybrid_rerank 可归因（本次重排真的成功了）",
             f"实际={sorted(labels['hybrid_rerank'])}（若为 hybrid 说明走的是降级路径）")

    # C6：C 配置必须带 rerank_score，且是**数值**（不是 None）
    scores = [r.get("rerank_score") for r in out["hybrid_rerank"]]
    ck.check(all(isinstance(s, float) for s in scores), "C6[c] 每条都带数值型 rerank_score",
             f"实际={scores}")

    # C7：B 与 C 的融合窗口必须不同（B=5、C=20）—— 这是最容易写错的一处
    fused20 = await hybrid_search(query, top_k=20)
    fused5 = await hybrid_search(query, top_k=5)
    ck.check(len(fused20) >= len(fused5) and len(fused20) > 5,
             "C7 融合窗口可独立控制（C 用 20 进重排，B 只用 5）",
             f"top20={len(fused20)} top5={len(fused5)}")

    # C8：A 配置这次改动 —— 从 D17 起 pure_vector 也应带相似度（不是 None）
    ck.check(all(r.get("similarity") is not None for r in out["pure_vector"]),
             "C8[a] pure_vector 的 similarity 全部非 None")
    print()
    return out


# ============================================================
# D 重排真的生效
# ============================================================
async def section_d(ck: Checker, query: str) -> None:
    print("\n" + "=" * 72)
    print("D 重排真的生效（不是「带了个字段但顺序没动」）")
    print("=" * 72)

    fused = await hybrid_search(query, top_k=20)
    ck.check(len(fused) > 1, "D0 融合候选数 > 1（否则「顺序没变」毫无信息量）", f"实际={len(fused)}")

    before = [item["chunk_id"] for item in fused]
    ordered, ok = await rerank(query, fused, top_n=len(fused))

    ck.check(ok, "D1 重排成功返回（reranked=True）")
    ck.check(len(ordered) == len(fused), "D2 全量重排条数不变",
             f"before={len(fused)} after={len(ordered)}")

    after = [item["chunk_id"] for item in ordered]
    moved = sum(1 for i, cid in enumerate(after) if before[i] != cid)
    print(f"  融合序前 5 名次：{[c[:8] for c in before[:5]]}")
    print(f"  重排后前 5 名次：{[c[:8] for c in after[:5]]}")
    print(f"  位置被改动的条数：{moved}/{len(before)}")
    ck.check(moved > 0, "D3 重排确实改变了顺序（moved > 0）",
             "全 20 条位置一个都没动 —— 需人工确认 rerank 是否真的在算")

    scores = [item["rerank_score"] for item in ordered]
    ck.check(all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)),
             "D4 重排分严格不增（降序正确）",
             f"实际={[round(s, 3) for s in scores[:6]]}…")

    # D5：重排分**不是** similarity —— 证明没有把同一个数塞进两个字段
    both = [(r["rerank_score"], r.get("similarity")) for r in ordered if r.get("similarity") is not None]
    ck.check(len(both) > 0, "D5-pre 存在同时有 rerank_score 与 similarity 的条目", f"实际={len(both)}")
    ck.check(any(rr != sim for rr, sim in both),
             "D5 重排分与 similarity 是两个不同的量纲（没有互相冒充）",
             f"样本={both[:3]}")

    top5_changed = len({c for c in before[:5]} - {c for c in after[:5]})
    print(f"  ℹ 前 5 集合的替换条数 = {top5_changed}（仅供参考：为 0 也可能只是这批数据恰好一致）")
    print()


# ============================================================
# E 降级可归因
# ============================================================
async def section_e(ck: Checker, query: str) -> None:
    print("\n" + "=" * 72)
    print("E 降级可归因（降级后必须能被认出「这条不是重排产出的」）")
    print("=" * 72)

    original = rerank_service._rerank_sync

    # ---- E1：注入"推理抛异常" ----
    def _boom(q: str, docs: list[str]) -> list[float]:
        raise RuntimeError("注入的推理故障")

    rerank_service._rerank_sync = _boom
    try:
        results = await retrieve(query, config="hybrid_rerank")
    finally:
        rerank_service._rerank_sync = original

    ck.check(len(results) > 0, "E1 降级后仍返回结果（没有 500）", f"实际={len(results)}")
    ck.check(all(r["retriever"] == "hybrid" for r in results),
             "E2 降级后 retriever 全部标为 hybrid（不是 hybrid_rerank）",
             f"实际={sorted({r['retriever'] for r in results})}")
    ck.check(all(r.get("rerank_score") is None for r in results),
             "E3 降级后 rerank_score 为 None（没有伪造一个分数）",
             f"实际={[r.get('rerank_score') for r in results]}")

    # ---- E2：注入"推理超时" ----
    def _slow(q: str, docs: list[str]) -> list[float]:
        time.sleep(1.0)
        return [0.0] * len(docs)

    original_timeout = settings.rerank_timeout_s
    rerank_service._rerank_sync = _slow
    settings.rerank_timeout_s = 0.1
    try:
        started = time.perf_counter()
        results = await retrieve(query, config="hybrid_rerank")
        elapsed = time.perf_counter() - started
    finally:
        rerank_service._rerank_sync = original
        settings.rerank_timeout_s = original_timeout

    ck.check(len(results) > 0, "E4 超时降级后仍返回结果")
    ck.check(all(r["retriever"] == "hybrid" for r in results),
             "E5 超时降级后 retriever 全部标为 hybrid",
             f"实际={sorted({r['retriever'] for r in results})}")
    ck.check(elapsed < 1.0,
             "E6 超时保护的是**请求延迟**（请求在 0.1s 阈值附近返回，没等满 1.0s）",
             f"实际耗时={elapsed:.3f}s")
    print(f"  ℹ 注意 E6 只说明请求提前返回了；被丢下的工作线程仍会把 1.0s 睡完再丢弃结果")
    print(f"    （Python 线程无法被强制中断 —— 超时不保护 CPU）")

    # ---- E3：降级后顺序 = 融合序，不是重排序 ----
    fused = await hybrid_search(query, top_k=5)
    ck.check([r["chunk_id"] for r in results] == [r["chunk_id"] for r in fused],
             "E7 降级返回的就是融合序前 5（没有别的东西混进来）")
    print()


# ============================================================
# F 上层契约（工具层 / 引用收集器）
# ============================================================
async def section_f(ck: Checker, query: str) -> None:
    print("\n" + "=" * 72)
    print("F 上层契约：工具层文本 + 引用收集器")
    print("=" * 72)

    reset_sources()                       # 每个请求入口都要重新绑定（见 citation.py）
    text_out = await search_documents(query)
    sources = get_sources()

    ck.check("[1]" in text_out, "F1 文本带引用编号 [1]")
    ck.check("None" not in text_out,
             "F2 格式化文本里不出现 'None'（D19 修的就是这个：BM25-only 条目 similarity=None）")
    ck.check(len(sources) > 0, "F3 引用收集器收到结构化来源", f"实际={len(sources)}")
    ck.check(all("similarity" in s and "source" in s and "content" in s for s in sources),
             "F4 收集器条目字段齐全（前端靠这三个字段渲染引用）")
    new_similarity_none = [s["index"] for s in sources if s["similarity"] is None]
    print(f"  ℹ 结构通道里 similarity 为 None 的编号 = {new_similarity_none or '无'}"
          f"（有 None 是正常的：那几条只被 BM25 命中）")
    print(f"  文本样例（前 80 字）：{text_out[:80]}…")

    # ---- F5/F6：直接对「similarity=None」这条分支做单元级验证 ----
    # 为什么必须单独做：上面 F1-F4 用的是**真实检索结果**，而本脚本语料只有 24 片、
    # 向量路 top20 恰好覆盖了 BM25 命中的那 3 片 → 本次压根**没出现** None。
    # 「没坏」不等于「验过」—— 靠真实数据撞到这条分支是碰运气，
    # 所以这里直接把一条 similarity=None 的结果喂进工具层。
    original_retrieve = tool_module.retrieve

    async def _fake_retrieve(query: str, *args, **kwargs) -> list[dict]:
        return [
            {
                "chunk_id": "fake-1",
                "content": "伪造片段：仅被 BM25 命中，向量路没召回到它。",
                "source": "fake.docx",
                "page_ref": None,
                "similarity": None,          # ← 就是这个值会渲染成"相关度 None"
                "retriever": "hybrid",
                "bm25_score": 3.2,
            }
        ]

    tool_module.retrieve = _fake_retrieve
    try:
        reset_sources()
        text_none = await search_documents(query)
        none_sources = get_sources()
    finally:
        tool_module.retrieve = original_retrieve

    ck.check("None" not in text_none,
             "F5 similarity=None 时格式化文本也不出现 'None'（把这条分支直接打出来）",
             f"实际文本={text_none!r}")
    ck.check(len(none_sources) == 1 and none_sources[0]["similarity"] is None,
             "F6 结构通道如实保留 similarity=None（不为了好看伪造一个 0）",
             f"实际={none_sources}")
    print()


# ============================================================
# G 延迟：B vs C（必须连 load 一起报）
# ============================================================
# 抖动百分比的**噪声地板**（毫秒）：样本低于这个量级时，"抖动 X%" 反映的是
# 进程调度噪声而不是机器负载 —— 报出来反而误导。
# 实测教训：B 配置只要 4~6ms，三次分别是 [6.5, 4.2, 4.5] → 脚本算出"抖动 54.4%"，
# 看起来像机器被抢核，其实只是毫秒级测量本身就不稳定。
# 铁律 11「结论必须数据驱动」的延伸：**给一个数字，就必须给出它的适用边界**。
JITTER_MEANINGFUL_MS = 50.0


async def section_g(ck: Checker, query: str) -> None:
    print("\n" + "=" * 72)
    print(f"G 端到端延迟 B vs C   [{load_tag()}]")
    print("=" * 72)

    # 预热：排除首次模型加载（那是 634ms 的一次性成本，不该混进单次检索耗时）
    await retrieve(query, config="hybrid_rerank")

    samples: dict[str, list[float]] = {}
    for name in ("hybrid", "hybrid_rerank"):
        times: list[float] = []
        for _ in range(3):
            started = time.perf_counter()
            await retrieve(query, config=name)
            times.append((time.perf_counter() - started) * 1000)
        samples[name] = times
        # ⚠ 抖动百分比只在样本本身够长时才有意义：
        #   B 配置只要 4~6ms，调度抖动就能让它看起来"抖动 54%"——
        #   那个数字是**测量噪声**，不是机器被抢核。给一个数字就必须给它的适用边界。
        if min(times) < JITTER_MEANINGFUL_MS:
            jitter_note = f"抖动无意义（样本 {min(times):.1f}ms < {JITTER_MEANINGFUL_MS}ms 噪声地板）"
        else:
            spread = (max(times) - min(times)) / min(times) * 100
            jitter_note = f"抖动 {spread:.1f}%"
        print(f"  {name:14s} {[round(t, 1) for t in times]}  {jitter_note}")

    best_b, best_c = min(samples["hybrid"]), min(samples["hybrid_rerank"])
    delta = best_c - best_b
    ratio = best_c / best_b if best_b > 0 else 0.0
    print(f"  → 重排这一步的净成本 ≈ {delta:.0f}ms（{ratio:.2f}×）   [{load_tag()}]")

    spread_all = max(samples["hybrid_rerank"]) / min(samples["hybrid_rerank"])
    if spread_all > 1.5:
        print("  ⚠ 本批 C 配置抖动 > 50%：机器在抢核，这组数只能当**上界**（铁律 11）")
    else:
        print("  ✓ 本批抖动正常，可作为基准；⚠ 但必须连同上面的 load 一起记，否则不可解释")

    ck.check(best_c > best_b, "G1 C 配置比 B 配置慢（重排的成本是真实存在的）",
             f"B={best_b:.1f}ms C={best_c:.1f}ms")
    print()


# ============================================================
# 主流程
# ============================================================
async def cleanup() -> int:
    async with async_session_factory() as session:
        rows = (await session.execute(
            text("SELECT id FROM documents WHERE filename LIKE :p"), {"p": f"{VERIFY_PREFIX}%"}
        )).all()
    for row in rows:
        await delete_document(row.id)
    return len(rows)


async def main() -> None:
    ck = Checker()

    section_a(ck)                                   # 不连库，先跑

    stale = await cleanup()
    if stale:
        print(f"（已清理上次残留的 {stale} 份测试文档）\n")

    # ---- B 语料自建（跑完即清，不污染全库 —— 用户的决定）----
    print("=" * 72)
    print("B 语料自建：每篇 ~730 字 → 切成 ~300 字的生产尺寸切片")
    print("=" * 72)
    total_chunks = 0
    lengths: list[int] = []
    counts: list[int] = []
    for i, (slug, keyword) in enumerate(TOPICS):
        content = build_doc(keyword, seed=1000 + i)
        filename = f"{VERIFY_PREFIX}-{slug}"
        _, count = await ingest_texts([content], filename=filename)
        total_chunks += count
        lengths.append(len(content))
        counts.append(count)
        print(f"  入库 {filename:30s} 原文 {len(content):4d} 字 → {count} 个切片")
    print(f"  合计 {len(TOPICS)} 篇 / {total_chunks} 个切片")

    ck.check(total_chunks > 20,
             "B1 切片数 > 20（否则测不出「20 候选 → 重排 → 5」这条链路）",
             f"实际={total_chunks}")
    # ⚠ B2 的第一版写成 `600 <= n <= 900`，实测只有 561~631 字 → 直接红了。
    #    红得对：那是**我的估算错了**，不是断言太严。修法是把生成器改成
    #    "拼到 ≥700 字再停"，而不是把区间放宽 —— 放宽就等于默认"短一点也行"，
    #    而这一段的整个意义就是"必须用生产尺寸的输入测"（铁律 11）。
    ck.check(all(TARGET_DOC_CHARS <= n <= 745 for n in lengths),
             f"B2 每篇原文都拼到生产量级尺寸（≥{TARGET_DOC_CHARS} 字）",
             f"实际长度={lengths}")
    ck.check(all(c == 3 for c in counts),
             "B3 每篇恰好切成 3 片（700~745 字 + step=250 的几何结果）",
             f"实际片数={counts}")

    async with async_session_factory() as session:
        row = (await session.execute(text(
            "SELECT avg(length(content))::float, max(length(content))::int, "
            "       min(length(content))::int "
            "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.filename LIKE :p"
        ), {"p": f"{VERIFY_PREFIX}%"})) .one()
    avg_len, max_len, min_len = row[0], row[1], row[2]
    print(f"  实际切片长度：平均 {avg_len:.0f} 字 / 最长 {max_len} / 最短 {min_len}")
    ck.check(avg_len >= 250 and max_len <= 310,
             "B4 实际切片尺寸 ≈ CHUNK_SIZE(300)（生产口径，不是 12 token 的玩具输入）",
             f"avg={avg_len:.0f} max={max_len}")
    ck.check(min_len >= 180,
             "B5 没有出现几十字的碎片切片（尾部碎片会把平均长度拖下来、也会影响 BM25 的 dl）",
             f"实际最短={min_len}")
    print()

    query = "灰度回滚"
    print(f"统一用 query={query!r}（只出现在 ops 那一篇里，BM25 命中集合可预期）")

    await section_c(ck, query)
    await section_d(ck, query)
    await section_e(ck, query)
    await section_f(ck, query)
    await section_g(ck, query)

    removed = await cleanup()

    # 清理断言：验证脚本自己造的数据，必须自己收干净
    async with async_session_factory() as session:
        left = (await session.execute(text(
            "SELECT count(*) FROM documents WHERE filename LIKE :p"
        ), {"p": f"{VERIFY_PREFIX}%"})) .scalar()
        # ⚠ 这两条查询必须写在**同一个 async with 里**。
        #   第一版把下面这条写到了 with 块外面（缩进少一层），
        #   session 在块尾已经 close，再 execute 会从池子里**重新借一条连接**，
        #   而这条连接没有任何人负责归还 —— 退出时 GC 才发现在关闭的 loop 上
        #   去终止它，于是打出一串 `RuntimeError: greenlet is being finalized` +
        #   `non-checked-in connection`。断言全绿、退出码 0，只有告警：
        #   典型的「不报错但资源漏了」（同族坑见铁律 12）。
        others = (await session.execute(text(
            "SELECT count(*) FROM documents WHERE filename NOT LIKE :p"
        ), {"p": f"{VERIFY_PREFIX}%"})) .scalar()

    print(f"（已清理本脚本造的 {removed} 份测试文档）")
    ck.check(left == 0, "H1 清理干净（本脚本范围内 0 残留）", f"实际残留={left}")
    print(f"  ℹ 非本脚本的既有文档数 = {others}（未被本脚本触碰）")

    await engine.dispose()
    ck.summary()


if __name__ == "__main__":
    asyncio.run(main())
