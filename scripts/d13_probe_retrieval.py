"""
D13 原理探针：BM25 / tsvector 的能力边界
==========================================
本脚本**只做探测，不做检索**（正式检索实现是 D16 的事）。
它把 D13 原理课上那几条"假设"变成跑得出来的数字：

  ① 当前 PostgreSQL 能不能直接做中文全文检索？          → 不能
  ② 应用层预分词 + simple 配置能不能恢复中文检索？      → 能，但有前提
  ③ ts_rank 到底有没有长度归一化？它和 BM25 是一回事吗？ → 不是
  ④ BM25 和 ts_rank 排出来的顺序一样吗？                → 不一样（本探针的核心结论）

副作用说明（刻意控制）：
- 只使用 `CREATE TEMP TABLE`（会话级临时表，连接断开即消失，不落盘、不改表结构）
- 不创建任何扩展、不改任何业务表

运行：
    .venv/bin/python -m scripts.d13_probe_retrieval
"""

import asyncio
import math

from sqlalchemy import text

from app.core.db import async_session_factory

# ---- BM25 参数（D16 实现须与此保持一致，否则排序结果无法对照）----
K1 = 1.2     # 词频饱和：控制"同一个词在同一篇里重复出现"的边际收益上限
B = 0.75     # 长度归一化：控制"长文档天然占优"要被压掉多少（0=不管，1=全按长度压）

# ---- 探针 ④ 用的语料：刻意构造出"关键词堆砌 vs 短而精"的对撞 ----
PROBE_DOCS = [
    ("智能 智能 智能 智能 智能 " + " ".join(["其他"] * 15), "关键词堆砌：tf=5，但文档长（20 词）"),
    ("智能 学习", "短而精：tf=1，文档仅 2 词"),
    ("学习 学习 学习", "不含查询词"),
    ("智能 学习 学习 学习", "tf=1，文档 4 词"),
]

# ---- 探针 ④ 的核心 SQL：用 PG 内建函数算出真 BM25（零扩展）----
# 逐块说明：
#   docs   —— 取出每个切片的 token 串，并算出文档长度 dl（token 个数）
#   q      —— 把查询拆成词序列（D16 里这一步是 jieba 分词的结果）
#   corpus —— 语料统计：N（文档数）与 avgdl（平均文档长度）—— BM25 的两个全局量
#   tf     —— 每个 (文档, 查询词) 的 f（词频），直接从 token 串里数
#   df     —— 每个查询词的 df（含该词的文档数）—— IDF 的输入
#   scored —— 套 BM25 公式对每个词求和
#            score = Σ IDF(q) × f×(k1+1) / ( f + k1×(1 - b + b×dl/avgdl) )
#            IDF(q) = ln(1 + (N - df + 0.5) / (df + 0.5))
#   注意：这里 N / avgdl / df 是"每次查询现算"（全表扫）。小数据量无所谓，
#        数据量上来后必须改为维护统计表或缓存 —— 这正是 pg_textsearch 要
#        自建专用索引（memtable 存语料统计）的原因。
#
#   ⚠️ 参数上那两处 CAST 不是多余的 —— 它们是本脚本第一次 run 时踩出来的坑：
#      `:k1 + 1` 里 PostgreSQL 会把 `:k1` 推断成 **integer**，于是 1.2 被静默截断成 1；
#      `1 - :b` 同理把 0.75 截断成 **0** —— 而 b=0 意味着「长度归一化被完全关掉」，
#      探针要测的东西就整个失效了，但脚本既不报错、也照样能跑完。
#      症状：BM25 与 ts_rank 的排序结果变得一模一样（两列都是 [1,2,4,3]）。
#      所以：**SQL 里的浮点参数一律显式 CAST，别指望类型推断。
#      外加 probe4 里的「参数自检 + 属性断言」把这类静默失效变成显式失败。**
BM25_SQL = text("""
WITH docs AS (
  SELECT id, tokens, array_length(string_to_array(tokens, ' '), 1)::float AS dl
  FROM _d13_probe
),
q AS (
  SELECT unnest(string_to_array(:q, ' ')) AS term
),
corpus AS (
  SELECT count(*)::float AS n, avg(dl) AS avgdl FROM docs
),
tf AS (
  SELECT d.id, q.term, d.dl,
         (SELECT count(*) FROM unnest(string_to_array(d.tokens, ' ')) AS t
          WHERE t = q.term)::float AS f
  FROM docs d CROSS JOIN q
),
df AS (
  SELECT term, count(*)::float AS df FROM tf WHERE f > 0 GROUP BY term
),
scored AS (
  SELECT tf.id, tf.f,
         sum( ln(1 + (c.n - df.df + 0.5) / (df.df + 0.5))
              * (tf.f * (CAST(:k1 AS double precision) + 1))
              / (tf.f + CAST(:k1 AS double precision)
                 * (1 - CAST(:b AS double precision)
                      + CAST(:b AS double precision) * tf.dl / c.avgdl)) ) AS bm25
  FROM tf JOIN df USING (term) CROSS JOIN corpus c
  GROUP BY tf.id, tf.f
)
SELECT b.id, s.f AS tf, s.bm25,
       ts_rank(to_tsvector('simple', b.tokens), to_tsquery('simple', :q)) AS ts_rank_score
FROM scored s JOIN _d13_probe b ON b.id = s.id
ORDER BY s.bm25 DESC
""")


def _idf(n: float, df: float) -> float:
    """IDF = ln(1 + (N - df + 0.5) / (df + 0.5))。越稀有的词分越高。"""
    return math.log(1 + (n - df + 0.5) / (df + 0.5))


async def probe1_chinese_tokenizer(session) -> None:
    print("=" * 74)
    print("① 当前数据库能不能直接做中文全文检索？")
    print("=" * 74)
    version = (await session.execute(text("SHOW server_version"))).scalar()
    exts = (await session.execute(text("SELECT extname FROM pg_extension ORDER BY 1"))).all()
    available = (await session.execute(text(
        "SELECT name FROM pg_available_extensions "
        "WHERE name IN ('zhparser', 'pg_bigm', 'pg_jieba', 'pg_trgm', 'unaccent') ORDER BY 1"
    ))).all()

    print(f"PostgreSQL 版本：{version}")
    print(f"已装扩展      ：{', '.join(r[0] for r in exts)}")
    print(f"可装的相关扩展：{', '.join(r[0] for r in available) or '无'}")

    row = (await session.execute(text(
        "SELECT to_tsvector('simple', '人工智能与机器学习')::text, "
        "       to_tsvector('simple', '人工智能与机器学习') @@ to_tsquery('simple', '智能')"
    ))).first()
    print(f"\nto_tsvector('simple', '人工智能与机器学习') = {row[0]}")
    print(f"用「智能」去查 → {row[1]}   ← 整句被当成一个 token，查不到")
    print("\n结论：内置分词器面向「空格分词」的语言；中文没有空格 → 整句一个 token。")
    print("      当前环境没有任何中文分词扩展 → 直接照 PRD 字面实现会卡死。")
    print()


async def probe2_pretokenized(session) -> None:
    print("=" * 74)
    print("② 应用层预分词 + simple 配置，能恢复中文检索吗？")
    print("=" * 74)
    tokens = "人工 智能 机器 学习 与 深度 学习"   # 相当于 D16 里 jieba 的输出
    row = (await session.execute(text(
        "SELECT to_tsvector('simple', :t)::text, "
        "       to_tsvector('simple', :t) @@ to_tsquery('simple', '智能'), "
        "       to_tsvector('simple', :t) @@ to_tsquery('simple', '人工智能')"
    ), {"t": tokens})).first()

    print(f"预分词结果：{tokens!r}")
    print(f"to_tsvector 结果：{row[0]}")
    print(f"查「智能」      → {row[1]}   ← 子串级能命中")
    print(f"查「人工智能」  → {row[2]}   ← 整词查不到（索引侧已被切成 人工/智能）")
    print("\n结论：可行 —— 数据库不需要「懂中文」，它只需要输入里已经有空格。")
    print("      但**索引侧与查询侧必须用同一个分词器**：只要一边不切或换词典，")
    print("      token 就对不上，检索会静默变差且不报错（这类无约束兜底的风险，")
    print("      与 D12 那个「外键写在模型里但库里没建出来」是同一类）。")
    print()


async def probe3_ts_rank_normalization(session) -> None:
    print("=" * 74)
    print("③ ts_rank 有没有长度归一化？它和 BM25 是一回事吗？")
    print("=" * 74)
    short_doc = "a b c"                                  # 3 个词
    long_doc = " ".join(list("abcdefghijklmnop"))        # 16 个词
    print(f"短文档（3 词）  ：{short_doc!r}")
    print(f"长文档（16 词） ：{long_doc!r}")
    print("查询词统一为 'a'（保证 tf 相同，唯一变量是文档长度）\n")

    notes = {
        0: "默认，无长度归一化",
        1: "按 1+log(长度)",
        2: "按长度",
        8: "按唯一词数",
        16: "按 1+log(唯一词数)",
        32: "按 自身+1（单调变换，不改排序）",
    }
    print(f"{'normalization':>14} | {'短文档':>10} | {'长文档':>10} | 说明")
    print("-" * 74)
    for norm in (0, 1, 2, 8, 16, 32):
        row = (await session.execute(text(
            "SELECT ts_rank(to_tsvector('simple', :s), to_tsquery('simple', 'a'), :n), "
            "       ts_rank(to_tsvector('simple', :l), to_tsquery('simple', 'a'), :n)"
        ), {"s": short_doc, "l": long_doc, "n": norm})).first()
        flag = "← 两篇完全相同" if row[0] == row[1] else "← 有长度差异"
        print(f"{norm:>14} | {row[0]:>10.6f} | {row[1]:>10.6f} | {notes[norm]} {flag}")

    print("\n结论 1：默认参数（0）下**没有**长度归一化——两篇分数一模一样。")
    print("结论 2：开 1/2/8/16 确实能加长度惩罚，但公式整体仍**不是 BM25**。")
    print("结论 3：硬理由是结构性的——ts_rank(tsvector, tsquery) 只接收这两个入参，")
    print("        拿不到 df / N / avgdl，所以 IDF 在数学上就不可能算出来。")
    print("        （面试话术：「用 tsvector 做 BM25」不准确 —— tsvector 是索引结构，")
    print("          BM25 是打分公式，PostgreSQL 没有内置 BM25。）")
    print()


async def probe4_bm25_vs_ts_rank(session) -> None:
    print("=" * 74)
    print("④ BM25 与 ts_rank 的排序结果对比（PG 内建 SQL，零扩展）")
    print("=" * 74)
    query = "智能"

    await session.execute(text("DROP TABLE IF EXISTS _d13_probe"))
    await session.execute(text("CREATE TEMP TABLE _d13_probe(id int, tokens text)"))
    for idx, (tokens, _) in enumerate(PROBE_DOCS, start=1):
        await session.execute(
            text("INSERT INTO _d13_probe VALUES (:i, :t)"), {"i": idx, "t": tokens}
        )

    # ---- 参数自检：确认 k1 / b 真的以浮点值送达数据库 ----
    # 这一步是被真实 bug 逼出来的：不加 CAST 时 `:k1 + 1` 会把参数推断成 integer，
    # 1.2 → 1、0.75 → 0，b=0 直接把长度归一化关掉 —— 而脚本照样跑完、不报任何错。
    effective = (await session.execute(text(
        "SELECT CAST(:k1 AS double precision) + 1, 1 - CAST(:b AS double precision)"
    ), {"k1": K1, "b": B})).first()
    assert abs(effective[0] - (K1 + 1)) < 1e-12 and abs(effective[1] - (1 - B)) < 1e-12, (
        f"参数未按浮点送达：k1+1={effective[0]}、1-b={effective[1]}，"
        f"期望 {K1 + 1} 与 {1 - B} —— 检查 SQL 里是否漏了 CAST"
    )

    rows = (await session.execute(
        BM25_SQL, {"q": query, "k1": K1, "b": B}
    )).all()

    stats = (await session.execute(text(
        "SELECT count(*)::float, avg(array_length(string_to_array(tokens, ' '), 1))::float "
        "FROM _d13_probe"
    ))).first()
    df = (await session.execute(text(
        "SELECT count(*) FROM _d13_probe WHERE :q = ANY(string_to_array(tokens, ' '))"
    ), {"q": query})).scalar()

    print(f"语料统计：N={stats[0]:.0f}  avgdl={stats[1]:.2f}  df(「{query}」)={df}  "
          f"IDF={_idf(stats[0], df):.4f}    参数：k1={K1}  b={B}")
    print(f"参数自检：数据库实际收到 k1+1={effective[0]}、1-b={effective[1]:.4f}（与脚本常量一致）\n")

    print(f"{'id':>3} {'tf':>4} {'BM25':>10} {'排':>3} | {'ts_rank':>10} {'排':>3} | 文档")
    print("-" * 74)
    ts_rank_order = sorted(rows, key=lambda r: -r[3])
    for rank_bm25, r in enumerate(rows, start=1):
        rank_ts = [x[0] for x in ts_rank_order].index(r[0]) + 1
        print(f"{r[0]:>3} {r[1]:>4.0f} {r[2]:>10.5f} {rank_bm25:>3} | "
              f"{r[3]:>10.5f} {rank_ts:>3} | {PROBE_DOCS[r[0] - 1][1]}")

    bm25_order = [r[0] for r in rows]
    ts_order = [r[0] for r in ts_rank_order]
    print(f"\nBM25 顺序   ：{bm25_order}")
    print(f"ts_rank 顺序：{ts_order}")

    # ---- 属性断言：把"参数没生效"这类静默失效变成显式失败 ----
    # 预期性质：tf=1 的短文档（id=2）应当胜过 tf=5 但长 10 倍的那篇（id=1）。
    # b 一旦被截断成 0（长度归一化失效），这条断言就会失败 —— 那正是我们要它失败的时候。
    assert bm25_order[0] == 2, (
        f"BM25 未把「短而精」排在第 1（实际顺序 {bm25_order}）—— "
        f"通常意味着 k1 / b 没有真正生效，先查 SQL 里的 CAST"
    )

    if bm25_order == ts_order:
        print("结论：两者顺序**相同** —— 与预期不符，先回去查参数是否生效，别照抄本段输出。")
    else:
        print(f"结论：两者顺序不同（BM25 第 1 名 = id {bm25_order[0]}，"
              f"ts_rank 第 1 名 = id {ts_order[0]}）。")
        print("      ts_rank 被「tf 刷 5 遍的长文档」骗到第 1 名；")
        print("      BM25 靠 tf 饱和（k1）+ 长度归一化（b）把它压下去。")
        print("      → 这就是 k1 / b 在真实排序上的效果，不是纸面概念。")
    print()


async def main() -> None:
    async with async_session_factory() as session:
        await probe1_chinese_tokenizer(session)
        await probe2_pretokenized(session)
        await probe3_ts_rank_normalization(session)
        await probe4_bm25_vs_ts_rank(session)
    print("=" * 74)
    print("探测完成（未改动任何表结构与扩展）")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
