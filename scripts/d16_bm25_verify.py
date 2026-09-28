"""
D16 验证脚本：BM25 关键词检索
==============================
按 D16 原理课列出的验收标准逐条验证。跑法：

    cd <项目根> && ~/.local/bin/uv run python -m scripts.d16_bm25_verify

五段：
  A 结构自检    —— 列/生成列/索引真的在库里（不信"语句没报错"）
  B 分词一致性  —— 单点分词、清洗规则、库中数据与重算一致、两侧 self-match
  C 检索功能    —— 命中正确 / 不误命中 / 长度归一化 / k1 饱和 / 契约 / 边界
  D 与向量对比  —— 两路命中集合差异（为 D17 的 RRF 留基线数据）
  E 口径对照    —— 全库统计量 vs 候选集统计量，排序是否真的会变

⚠️ 本脚本的关键设计：断言全部带**正向条件**（非空、非零）——
   否则"表里 0 行"这种空集合会让断言恒满足、假通过（铁律 9 的教训）。
⚠️ 对照实验的**变量必须控干净**（铁律 11 的教训）：
   要测长度归一化，两篇文档就必须 tf 相同、只有长度不同；
   要测词频饱和，就必须长度相近、只有 tf 不同。混在一起测出来的东西证明不了任何结论。
"""

import asyncio

from sqlalchemy import text

from app.core.db import async_session_factory, engine
from app.services.document_service import (
    count_documents_by_prefix,
    delete_documents_by_prefix,
    ingest_texts,
)
from app.services.retrieval_service import (
    BM25_B,
    BM25_K1,
    _BM25_SQL,
    search,
    search_keywords,
)
from app.services.tokenizer import build_or_tsquery, tokenize, tokenize_to_string

VERIFY_PREFIX = "d16-bm25-verify"

# 对照实验用的四份文档（刻意设计，见 C 段的断言说明）
VERIFY_DOCS = {
    # tf=1、短（dl 小）：作为「长度归一化」与「k1 饱和」两条断言的基准
    f"{VERIFY_PREFIX}-short": "报销流程说明。",
    # tf=1、长（dl 大）：与上面 tf 相同，**唯一变量是长度**
    f"{VERIFY_PREFIX}-long": "报销流程说明。"
    + "本公司各项规章制度均以员工手册为准，如有疑问请咨询人力资源部门。" * 6,
    # tf=10、长度与 short 相近：与 short **唯一变量是词频**
    f"{VERIFY_PREFIX}-tf10": "报销" * 10 + "流程说明。",
    # 对照组：完全不含"报销"，用于验证「不误命中」
    f"{VERIFY_PREFIX}-other": "代码上线前必须在灰度环境验证，出现故障需在十分钟内完成回滚。",
}


# E2 对照实验专用：把 N / avgdl / df 全部改成**只在候选集内**统计。
# 故意留在这个脚本里、不进 retrieval_service —— 它是「被否掉的方案」，
# 只用于对照，不该出现在生产代码路径上。
_BM25_SQL_CANDIDATE_SCOPE = text("""
WITH candidates AS (
    SELECT c.id, c.content_tokens, d.filename,
           array_length(string_to_array(c.content_tokens, ' '), 1)::float AS dl
    FROM document_chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.status = 'ready'
      AND c.content_tsv @@ to_tsquery('simple', :tsq)
),
corpus AS (
    SELECT count(*)::float AS n, avg(dl)::float AS avgdl FROM candidates
),
q AS (
    SELECT unnest(string_to_array(:terms, ' ')) AS term
),
tf AS (
    SELECT cd.id, q.term, cd.dl,
           (SELECT count(*)
            FROM unnest(string_to_array(cd.content_tokens, ' ')) AS t
            WHERE t = q.term)::float AS f
    FROM candidates cd CROSS JOIN q
),
df AS (
    SELECT q.term, count(*)::float AS df
    FROM q CROSS JOIN candidates cd
    WHERE q.term = ANY(string_to_array(cd.content_tokens, ' '))
    GROUP BY q.term
),
scored AS (
    SELECT tf.id,
           sum(
               ln(1 + (corpus.n - df.df + 0.5) / (df.df + 0.5))
               * (tf.f * (CAST(:k1 AS double precision) + 1))
               / (tf.f + CAST(:k1 AS double precision)
                        * (1 - CAST(:b AS double precision)
                             + CAST(:b AS double precision) * tf.dl / corpus.avgdl))
           ) AS score
    FROM tf
    JOIN df ON df.term = tf.term
    CROSS JOIN corpus
    GROUP BY tf.id
)
SELECT cd.filename, s.score
FROM scored s
JOIN candidates cd ON cd.id = s.id
ORDER BY s.score DESC, cd.id ASC
LIMIT :top_k
""")


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


# ============================================================
# A. 结构自检
# ============================================================
async def section_a(ck: Checker) -> None:
    print("=" * 74)
    print("A. 结构自检（回读数据库真实结构，不信「语句没报错」）")
    print("=" * 74)
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT column_name, data_type, is_generated
            FROM information_schema.columns
            WHERE table_name = 'document_chunks'
              AND column_name IN ('content_tokens', 'content_tsv')
        """))).all()
        found = {r.column_name: (r.data_type, r.is_generated) for r in rows}
        print(f"  库中列结构：{found}")

        ck.check(found.get("content_tokens") == ("text", "NEVER"),
                 "A1 content_tokens 是普通 text 列")
        ck.check(found.get("content_tsv") == ("tsvector", "ALWAYS"),
                 "A2 content_tsv 是 STORED 生成列")

        indexdef = (await session.execute(text("""
            SELECT indexdef FROM pg_indexes
            WHERE tablename = 'document_chunks' AND indexname = 'idx_chunks_tsv'
        """))).scalar()
        print(f"  倒排索引：{indexdef}")
        ck.check(bool(indexdef) and "gin" in (indexdef or "").lower(),
                 "A3 GIN 倒排索引 idx_chunks_tsv 存在")

        total = (await session.execute(text("""
            SELECT count(*) FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.status = 'ready'
        """))).scalar()
        missing = (await session.execute(text("""
            SELECT count(*) FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.status = 'ready' AND c.content_tokens IS NULL
        """))).scalar()
        print(f"  ready 切片共 {total} 行，其中未分词 {missing} 行")
        # 正向条件：total > 0 才算真的验证了「回填完整性」
        ck.check(total > 0 and missing == 0,
                 f"A4 ready 切片全部已分词（{total} 行，未分词 {missing} 行）")
    print()


# ============================================================
# B. 分词一致性
# ============================================================
async def section_b(ck: Checker) -> None:
    print("=" * 74)
    print("B. 分词一致性（索引侧与查询侧是同一份契约）")
    print("=" * 74)

    # B1 确定性：同一个输入两次结果必须一样
    sample = "员工年假天数按工龄计算，报销需审批。"
    first, second = tokenize(sample), tokenize(sample)
    print(f"  tokenize({sample!r}) = {first}")
    ck.check(first == second and len(first) > 0, "B1 分词确定且非空（两次结果一致）")

    # B2 清洗：tsquery 危险字符必须被处理掉 —— 断言打在「送进数据库不报错」这个行为上
    dirty = ["A&B公司", "3<5", "紧急!重要", "foo(bar)", "cost: 100", "a|b", "他说'你好'"]
    harmless = True
    async with async_session_factory() as session:
        for q in dirty:
            tsq = build_or_tsquery(tokenize(q))
            try:
                await session.execute(text("SELECT to_tsquery('simple', :q)"), {"q": tsq})
            except Exception as e:
                harmless = False
                await session.rollback()
                print(f"    {q!r} → tsq={tsq!r} 报错：{str(e).splitlines()[0][:60]}")
    ck.check(harmless, f"B2 含 tsquery 元字符的 {len(dirty)} 个查询全部可安全解析")

    # B3 库中存的分词结果 == 现在重新分词的结果（抓漏回填 / 手写库 / 换分词器未重跑）
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT id, coalesce(content, '') AS content, content_tokens
            FROM document_chunks ORDER BY id
        """))).all()
        bad = [
            r.id for r in rows
            if tokenize_to_string(r.content) != (r.content_tokens or "")
        ]
    ck.check(len(rows) > 0 and not bad,
             f"B3 库中 {len(rows)} 行分词结果与重算完全一致", f"不一致的行 id={bad}")

    # B4 两侧 self-match：token 在 tsvector 与 tsquery 里必须规范化成同一个 lexeme
    tokens = ["年假", "报销", "员工", "C++", "python", "1.2", "2024", "email"]
    mismatch = []
    async with async_session_factory() as session:
        for t in tokens:
            ok = (await session.execute(text(
                "SELECT to_tsvector('simple', :t) @@ to_tsquery('simple', :t)"
            ), {"t": t})).scalar()
            if not ok:
                mismatch.append(t)
    ck.check(not mismatch,
             f"B4 {len(tokens)} 个 token 的索引侧/查询侧规范化一致", f"不一致：{mismatch}")
    print()


# ============================================================
# C. 检索功能
# ============================================================
async def section_c(ck: Checker) -> None:
    print("=" * 74)
    print("C. 检索功能")
    print("=" * 74)

    # ---- 清掉上次残留的同名测试文档，保证幂等 ----
    # 2026-09-28 改：改用 delete_documents_by_prefix 统一口径（原先是手写 SQL +
    # 逐条 delete_document）。作用域一样是 VERIFY_PREFIX，只是收口到一处逻辑，
    # 顺带拿到 LIKE 通配符转义（前缀里出现下划线时手写 SQL 会误伤）。
    stale = await delete_documents_by_prefix(VERIFY_PREFIX)
    if stale:
        print(f"  （已清理上次残留的 {stale} 份测试文档）")

    # ---- 造数据 ----
    doc_ids: dict[str, str] = {}
    for filename, content in VERIFY_DOCS.items():
        doc_id, count = await ingest_texts([content], filename=filename)
        doc_ids[filename] = str(doc_id)
        print(f"  入库 {filename:28} 切片 {count} 个")

    async with async_session_factory() as session:
        stats = (await session.execute(text("""
            SELECT c.document_id,
                   length(c.content) AS chars,
                   array_length(string_to_array(c.content_tokens, ' '), 1) AS dl
            FROM document_chunks c
            WHERE c.document_id = ANY(:ids)
            ORDER BY c.document_id
        """), {"ids": [doc_ids[f] for f in VERIFY_DOCS]})).all()
        dl_map = {str(r.document_id): (r.chars, r.dl) for r in stats}
    for filename, doc_id in doc_ids.items():
        chars, dl = dl_map.get(doc_id, ("?", "?"))
        print(f"    {filename:28} 字符数={chars:>4}  dl(token数)={dl}")

    # ---- C1 命中正确的文档 ----
    hits = await search_keywords("报销", top_k=10)
    sources = [h["source"] for h in hits]
    print(f"\n  查「报销」→ {sources}")
    ck.check(len(hits) > 0, "C1a 查询有命中（非空）")
    ck.check(f"{VERIFY_PREFIX}-short" in sources, "C1b 命中 tf=1 的短文档")
    ck.check(f"{VERIFY_PREFIX}-tf10" in sources, "C1c 命中 tf=10 的文档")
    ck.check(f"{VERIFY_PREFIX}-other" not in sources,
             "C1d 不含查询词的文档**不**出现（②筛候选按 OR，语义是「至少命中一个词」）")

    # ---- C2 另一个查询命中另一组（证明不是「谁都命中」）----
    hits2 = await search_keywords("灰度回滚", top_k=10)
    sources2 = [h["source"] for h in hits2]
    print(f"  查「灰度回滚」→ {sources2}")
    # D17 修正：原断言是 all("other" in s for s in sources2)，
    # 隐含前提是「全库只有本脚本的测试文档」—— 库里一旦有别的文档
    # （哪怕它确实含"灰度/回滚"、本该被召回），断言就会红，
    # 而那是断言脆弱、不是代码坏了。
    # 修法：把范围限定到本脚本自己的语料，排除性照旧（other 命中、其余三篇落选）。
    own_sources2 = [s for s in sources2 if s.startswith(VERIFY_PREFIX)]
    others2 = [s for s in sources2 if not s.startswith(VERIFY_PREFIX)]
    ck.check(own_sources2 == [f"{VERIFY_PREFIX}-other"],
             "C2 本脚本语料里只命中运维文档，员工手册三篇全部落选",
             f"本脚本命中 {own_sources2}")
    if others2:
        print(f"  （库中另有 {len(others2)} 条外部文档命中，非本脚本语料，不计入断言：{others2[:3]}）")

    # ---- C3 长度归一化：tf 相同、长度不同 → 短的必须赢 ----
    short = next((h for h in hits if h["source"] == f"{VERIFY_PREFIX}-short"), None)
    long_ = next((h for h in hits if h["source"] == f"{VERIFY_PREFIX}-long"), None)
    if short and long_:
        print(f"\n  长度归一化（tf 均为 1，唯一变量是长度）：")
        print(f"    short(dl={dl_map[doc_ids[f'{VERIFY_PREFIX}-short']][1]})"
              f" score={short['score']}  >  "
              f"long(dl={dl_map[doc_ids[f'{VERIFY_PREFIX}-long']][1]})"
              f" score={long_['score']}")
        ck.check(short["score"] > long_["score"],
                 "C3 同样命中 1 次时，短文档得分高于长文档（b=0.75 生效）",
                 f"short={short['score']} long={long_['score']}")
    else:
        ck.check(False, "C3 长度归一化对照", "样本缺失，无法对照")

    # ---- C4 k1 饱和：长度相近、tf 相差 10 倍 → 分数增长必须远小于 10 倍 ----
    tf10 = next((h for h in hits if h["source"] == f"{VERIFY_PREFIX}-tf10"), None)
    if short and tf10:
        ratio = tf10["score"] / short["score"] if short["score"] else float("inf")
        print(f"\n  词频饱和（tf 1 → 10，长度相近）：")
        print(f"    score 比值 = {ratio:.3f}（k1={BM25_K1} 时理论上限是 k1+1 = {BM25_K1 + 1}）")
        ck.check(ratio < BM25_K1 + 1,
                 f"C4 词频涨 10 倍、分数只涨 {ratio:.2f} 倍（< k1+1={BM25_K1 + 1}，k1 饱和生效）",
                 f"比值 {ratio:.3f} 超过上限")
    else:
        ck.check(False, "C4 词频饱和对照", "样本缺失，无法对照")

    # ---- C5 返回契约 ----
    if hits:
        keys = set(hits[0].keys())
        # D17 起多一个 chunk_id：融合（RRF）要靠主键判断"两路是不是同一片"，
        # 精确集合断言的价值就在这里 —— 返回结构一变它立刻红，不会静默漏掉。
        expect_keys = {"chunk_id", "content", "source", "page_ref", "score", "retriever"}
        print(f"\n  返回字段：{sorted(keys)}")
        ck.check(keys == expect_keys, "C5a 返回字段完整且不含 similarity",
                 f"实际 {keys}")
        ck.check(all(isinstance(h["chunk_id"], str) and h["chunk_id"] for h in hits),
                 "C5a2 每条的 chunk_id 都是非空字符串（D17 融合的身份键）")
        ck.check(all(h["retriever"] == "bm25" for h in hits),
                 "C5b 每条的 retriever 都标为 bm25（供 D17 融合时区分来源）")
        ck.check(all(h["score"] > 0 for h in hits),
                 "C5c 分数全部为正（BM25 分数 > 0 才可能入选，0 分不会进候选）")

    # ---- C6 排序降序 ----
    scores = [h["score"] for h in hits]
    ck.check(scores == sorted(scores, reverse=True), "C6 结果按 score 降序")

    # ---- C7 top_k 生效 ----
    top2 = await search_keywords("报销", top_k=2)
    ck.check(len(top2) <= 2, f"C7 top_k=2 生效（实际返回 {len(top2)} 条）")

    # ---- C8 边界：纯标点查询清洗后为空 → 返回 []，且不报错 ----
    for weird in ["！！！", "   ", "&|<>"]:
        empty = await search_keywords(weird, top_k=3)
        ck.check(empty == [], f"C8 退化查询 {weird!r} 返回空列表而不报错")
    print()


# ============================================================
# D. 与向量检索的对比（为 D17 的 RRF 留基线）
# ============================================================
async def section_d(ck: Checker) -> None:
    print("=" * 74)
    print("D. BM25 vs 向量检索：同一查询的命中集合差异（D17 融合的基线数据）")
    print("=" * 74)
    for q in ["报销", "灰度回滚", "上线前要做什么"]:
        bm25 = await search_keywords(q, top_k=3)
        vec = await search(q, top_k=3)
        b_src = [h["source"].replace(f"{VERIFY_PREFIX}-", "") for h in bm25]
        v_src = [h["source"].replace(f"{VERIFY_PREFIX}-", "") for h in vec]
        same = b_src[:1] == v_src[:1]
        print(f"  查询 {q!r}")
        print(f"    BM25  : {b_src}")
        print(f"    向量   : {v_src}")
        print(f"    首名{'相同' if same else '**不同**'}")
    # D 段是「记录差异」，不做硬断言 —— 两路排序是否不同取决于语料，
    # 强行断言「必须不同」会变成造数据去迎合结论（铁律 11 的教训）。
    ck.check(True, "D1 两路检索均已跑通、结果已记录（差异本身不作断言）")
    print()


# ============================================================
# E. 口径对照：全库统计量 vs 候选集统计量
# ============================================================
async def section_e(ck: Checker) -> None:
    print("=" * 74)
    print("E. 口径对照：N/df 用「全库」还是「候选集」——排序会不会真的变？")
    print("=" * 74)

    query = "报销 灰度回滚"
    tokens = tokenize(query)
    print(f"  查询 {query!r} → tokens={tokens}")

    async with async_session_factory() as session:
        # 全库口径
        full = (await session.execute(text("""
            WITH q AS (SELECT unnest(string_to_array(:terms, ' ')) AS term)
            SELECT q.term,
                   count(*)::float AS df,
                   (SELECT count(*) FROM document_chunks c2
                     JOIN documents d2 ON d2.id = c2.document_id
                    WHERE d2.status='ready' AND c2.content_tokens <> '')::float AS n
            FROM q
            CROSS JOIN document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.status='ready' AND c.content_tokens <> ''
              AND q.term = ANY(string_to_array(c.content_tokens, ' '))
            GROUP BY q.term
        """), {"terms": " ".join(tokens)})).all()

        # 候选集口径（候选 = 至少命中一个查询词的切片）
        cand = (await session.execute(text("""
            WITH q AS (SELECT unnest(string_to_array(:terms, ' ')) AS term),
            candidates AS (
                SELECT c.id, c.content_tokens
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.status='ready'
                  AND c.content_tsv @@ to_tsquery('simple', :tsq)
            )
            SELECT q.term,
                   count(*)::float AS df,
                   (SELECT count(*) FROM candidates)::float AS n
            FROM q
            CROSS JOIN candidates cd
            WHERE q.term = ANY(string_to_array(cd.content_tokens, ' '))
            GROUP BY q.term
        """), {"terms": " ".join(tokens), "tsq": build_or_tsquery(tokens)})).all()

    import math

    def idf(n, df):
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    print(f"\n  {'口径':<10} {'term':<12} {'N':>4} {'df':>4} {'IDF':>8}")
    print("  " + "-" * 44)
    full_map, cand_map = {}, {}
    for r in full:
        full_map[r.term] = idf(r.n, r.df)
        print(f"  {'全库':<10} {r.term:<12} {r.n:>4.0f} {r.df:>4.0f} {idf(r.n, r.df):>8.4f}")
    for r in cand:
        cand_map[r.term] = idf(r.n, r.df)
        print(f"  {'候选集':<10} {r.term:<12} {r.n:>4.0f} {r.df:>4.0f} {idf(r.n, r.df):>8.4f}")

    common = set(full_map) & set(cand_map)
    if len(common) >= 2:
        full_spread = max(full_map[t] for t in common) / min(full_map[t] for t in common)
        cand_spread = max(cand_map[t] for t in common) / min(cand_map[t] for t in common)
        delta = cand_spread - full_spread
        direction = "放大" if delta > 0 else ("压缩" if delta < 0 else "不变")
        print(f"\n  词间 IDF 区分度（最大/最小）：全库口径 {full_spread:.3f}，"
              f"候选集口径 {cand_spread:.3f}")
        print(f"  → 候选集口径把区分度{direction}了 {abs(delta):.3f}")
        ck.check(True, "E1 两种口径的 IDF 差异已量化（结构性推理 → 实测数据）")
    else:
        direction = "无法判断"
        ck.check(True, "E1 术语数不足，仅记录数据")

    # ---- E2：两种口径各跑一遍**完整排序**，看顺序是否真的会变 ----
    # 只比较 IDF 数值是不够的 —— IDF 整体平移并不必然改变排序。
    async with async_session_factory() as session:
        params = {
            "terms": " ".join(tokens),
            "tsq": build_or_tsquery(tokens),
            "k1": BM25_K1,
            "b": BM25_B,
            "top_k": 10,
        }
        full_order = [r.filename for r in (await session.execute(_BM25_SQL, params)).all()]
        cand_order = [
            r.filename for r in (await session.execute(_BM25_SQL_CANDIDATE_SCOPE, params)).all()
        ]

    def brief(names: list[str]) -> list[str]:
        return [n.replace(f"{VERIFY_PREFIX}-", "") for n in names]

    print("\n  完整排序对比：")
    print(f"    全库口径  : {brief(full_order)}")
    print(f"    候选集口径: {brief(cand_order)}")
    order_same = full_order == cand_order
    ck.check(True, f"E2 两种口径排序{'一致' if order_same else '不同'}（如实记录，不预设）")

    print("\n  结论（由上面的数据得出，不预设方向）：")
    print("  ① N 变小 → 所有词的 IDF 同向变小，这一步方向可预测；")
    print("  ② 但词与词的**相对**权重结构怎么变，取决于 df 分布 ——")
    print(f"     本次实测是「{direction}」，与讲解时预判的「压缩」**不符**（以实测为准）；")
    print("  ③ 真正站得住的危害不是方向，而是：IDF 本应是**词的固有属性**（全语料稀有度），")
    print("     改用候选集算，它就变成了**本次查询的函数** —— 同一个词，换个查询、")
    print("     候选集一换权重就变，排序依据不稳定，也无法跨查询横向比较。")
    print()


async def main() -> None:
    ck = Checker()
    await section_a(ck)
    await section_b(ck)
    await section_c(ck)
    await section_d(ck)
    await section_e(ck)

    # ✅ 跑完把自己的语料收干净（2026-09-28 新增）。
    # 原先只在 C 段开头清"上次残留"，跑完就不管了 —— 结果库里长期躺着
    # 4 份 d16-bm25-verify-*，会被 D22/D23 的检索评测捞到。
    # 只删别人的不够，**留下自己的同样是污染**。
    removed = await delete_documents_by_prefix(VERIFY_PREFIX)
    left = await count_documents_by_prefix(VERIFY_PREFIX)
    ck.check(left == 0, f"Z1 本脚本语料已收干净（清 {removed} 份，残留 {left}）")

    await engine.dispose()
    ck.summary()


if __name__ == "__main__":
    asyncio.run(main())
