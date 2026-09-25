"""
检索服务（RAG 在线检索）
=========================
D12 拆表后本文件只管**读**：

    切片工具：split_text()   长文本 → 有重叠的片段（写路径也复用它）
    向量检索：search()        问题 → 向量 → 相似度排序 → top-k

入库（写）与文档状态管理已移到 app/services/document_service.py —— 按读写分职责。

端到端流程（用户提问时）：
  search(问题)
    → embed_query(问题)                          问题变向量
    → SQL: document_chunks JOIN documents
             WHERE documents.status = 'ready'    只检索已就绪的文档
             ORDER BY embedding <=> 问题向量       pgvector 余弦距离，越小越相似
             LIMIT top_k
    → 返回片段 + 文件名 + 页码                     供引用定位使用
    → 调用方拼进 prompt 给 LLM

D16 新增第二条腿 —— 关键词检索：

  search_keywords(问题)
    问题 → tokenize（与索引侧同一个分词器）→ tsquery（OR 连接）
        → 走 content_tsv 的 GIN 倒排**筛候选**            ← 第②层只管"找得到"
        → 对候选套 BM25 公式（N/df/avgdl 现算）排序         ← 第③层管"排得对"

  为什么需要这条路（向量检索的短板）：
    向量擅长"意思相近"，对**精确字面**不敏感。问「E104 型号」「3.5% 提成比例」
    这类问题时，向量可能把语义相近但字面无关的片段排到前面；BM25 恰好相反 ——
    字面不命中就是 0 分。两者命中集合不同，才值得融合（D17 的 RRF 做这件事）。

D17 新增第三条腿 —— 融合（RRF）：

  hybrid_search(问题)
    → 并发发起两路（asyncio.gather）：
         search(问题, top_k=20)            向量路
         search_keywords(问题, top_k=20)   BM25 路
    → fuse_rrf(...)                        只取名次、不问分数 → 一个有序列表
    → 返回 [{chunk_id, content, source, page_ref, rrf_score,
             vector_rank, bm25_rank, similarity, bm25_score, retriever}, ...]

  今天的边界：`app/tools/retrieval.py`（Agent 的工具层）**仍只走向量路**，
  检索策略配置化（pure_vector / hybrid / hybrid_rerank 三态可切）在 D19。
  所以本日交付的是"一条能被抽样对比的第二条管线"，Agent 行为不变。
"""

import asyncio

from sqlalchemy import select, text

from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.embedding_service import embed_query
from app.services.tokenizer import build_or_tsquery, tokenize

# ---- 切片配置 ----
CHUNK_SIZE = 300        # 每个切片的字符数
CHUNK_OVERLAP = 50      # 相邻切片重叠字符数（防止把一句话从中间切断）
DEFAULT_TOP_K = 3       # 默认检索返回条数


def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    把长文本切成有重叠的片段（Chunking）。

    为什么要重叠（overlap）：
      如果严格按 300 字切断，可能把一句话切成两半：
        "...北京是中国的" | "首都，人口众多..."
      两片单独看都语义不完整 → 检索到也帮不上 LLM
      重叠 50 字后，两片都包含完整句子

    关键几何关系：重叠 = chunk_size - step
      chunk_size 管「语义密度」，overlap 管「抗切断」，是两个独立的调优维度，
      所以代码里先算 step（循环实际迈多大步），再进循环。

    Args:
        text: 原始文本
        chunk_size: 每片字符数
        overlap: 相邻片重叠字符数（必须 < chunk_size）
    Returns:
        切片列表
    """
    if overlap >= chunk_size:
        raise ValueError("overlap 必须小于 chunk_size")

    text = text.strip()
    if len(text) <= chunk_size:
        return [text] if text else []

    chunks: list[str] = []
    step = chunk_size - overlap        # 每次前进的步长
    for start in range(0, len(text), step):
        chunk = text[start : start + chunk_size]
        if chunk.strip():
            chunks.append(chunk.strip())
        if start + chunk_size >= len(text):   # 已到末尾
            break
    return chunks


async def search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """
    向量检索：找与问题语义最相近的 top-k 片段。

    SQL 核心：ORDER BY embedding <=> query_vector
      <=> 是 pgvector 的**余弦距离**算子（值越小越相似）
      相似度 = 1 - 距离（转成「越大越相似」，好理解也好展示）

    为什么要 JOIN documents：
      文件名和文档状态在 documents 表里，切片表只存 document_id。
      顺带解决了「只检索已就绪文档」这件事 —— processing / failed 的文档
      切片要么残缺、要么本就不该存在，不过滤就可能命中半截内容。

    Args:
        query: 用户问题
        top_k: 返回条数
    Returns:
        [{"content":..., "source":..., "page_ref":..., "similarity":...}, ...]（按相似度降序）
    """
    # 两个阶段必须用同一个 embedding 模型：只有同一模型的向量才在同一语义空间
    query_vec = await embed_query(query)

    # 用 SQLAlchemy 表达 pgvector 的距离排序
    distance = DocumentChunk.embedding.cosine_distance(query_vec)
    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.content,
            DocumentChunk.page_ref,
            Document.filename,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == DocumentStatus.READY)
        .order_by(distance)          # 距离升序 = 相似度降序
        .limit(top_k)
    )

    async with async_session_factory() as session:
        rows = (await session.execute(stmt)).all()

    results = [
        {
            # D17：切片的唯一身份。融合（RRF）要判断"两路返回的这两条是不是同一片"，
            # 靠 content 字符串比对是脆的（空白/截断差异会静默配对失败，
            # 表现为"本该合并的一条变成两条并列"）。身份只能来自主键。
            "chunk_id": str(row.id),
            "content": row.content,
            # 沿用 "source" 这个键名：工具层与引用收集器都按它取来源展示名
            "source": row.filename,
            "page_ref": row.page_ref,
            "similarity": round(1 - row.distance, 4),   # 距离 → 相似度
        }
        for row in rows
    ]
    logger.info("检索完成 query=%r 命中=%d 条", query[:20], len(results))
    return results


# ============================================================
# D16：关键词检索（BM25）
# ============================================================
# BM25 参数。与 D13 探针（scripts/d13_probe_retrieval.py）保持**同一组值**，
# 这样探针的实测输出和这里的线上结果可以直接对照，出问题时能快速判断是
# "参数不同"还是"实现有 bug"。
BM25_K1 = 1.2     # 词频饱和：限制"同一个词在同一篇里重复出现"的边际收益
BM25_B = 0.75     # 长度归一化：控制"长文档天然占优"要被压掉多少

# BM25 打分的 SQL。逐块说明（CTE 名 → 职责）：
#
#   corpus     语料统计 N 与 avgdl —— **两个全库量**，只统计 ready 文档的切片
#   candidates ② 筛候选：走 GIN 倒排（content_tsv @@ tsquery），不扫全表
#   q          查询词序列（应用侧 jieba 清洗后的 token，空格分隔传入）
#   tf         每个 (候选, 查询词) 的词频 f，以及候选的文档长度 dl
#   df         每个查询词的 df —— 也是**全库量**，与 corpus 同口径
#   scored     套 BM25 公式：对每个候选按查询词求和
#
# 三个必须记住的点（都是踩过的坑）：
#
#   ① 浮点参数一律显式 CAST。不加 CAST，PostgreSQL 会把 :k1 推断成 integer，
#      1.2 被静默截断成 1、0.75 被截断成 0 —— 而 b=0 等于把长度归一化整个关掉。
#      脚本不报错、照样跑完，只是排序悄悄变错。（D13 坑 1）
#
#   ② N / avgdl / df 的口径必须一致，且必须覆盖**整个语料**。
#      只统计候选集会让 IDF 失真：候选集是"与查询相关"的子集，
#      里面的词天然更常见，"稀有度"被系统性低估 → 词间区分度被抹平。
#
#   ③ 候选筛选用 OR（tsquery 里 ' | '）不是风格选择，是正确性要求：
#      BM25 中不含任何查询词的文档分数恒为 0，所以"至少命中一个词"与
#      "分数可能非 0"等价 —— OR 的结果与扫全表完全一致；换成 AND 会踢掉
#      "只命中部分词"的文档（分数非 0），静默篡改排序。（见 tokenizer.build_or_tsquery）
_BM25_SQL = text("""
WITH corpus AS (
    SELECT count(*)::float AS n,
           avg(array_length(string_to_array(c.content_tokens, ' '), 1))::float AS avgdl
    FROM document_chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.status = 'ready'
      AND c.content_tokens IS NOT NULL
      AND c.content_tokens <> ''
),
candidates AS (
    SELECT c.id, c.content, c.page_ref, c.content_tokens, d.filename,
           array_length(string_to_array(c.content_tokens, ' '), 1)::float AS dl
    FROM document_chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.status = 'ready'
      AND c.content_tsv @@ to_tsquery('simple', :tsq)
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
    FROM q
    CROSS JOIN document_chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.status = 'ready'
      AND c.content_tokens IS NOT NULL
      AND c.content_tokens <> ''
      AND q.term = ANY(string_to_array(c.content_tokens, ' '))
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
SELECT cd.id, cd.content, cd.filename, cd.page_ref, s.score,
       -- 窗口函数在 LIMIT 之前求值，所以这里拿到的是「LIMIT 前的候选总数」，
       -- 用于日志观察 OR 的召回宽度；不影响返回条数
       count(*) OVER () AS candidate_count
FROM scored s
JOIN candidates cd ON cd.id = s.id
ORDER BY s.score DESC, cd.id ASC
LIMIT :top_k
""")


async def search_keywords(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """
    关键词检索（BM25）：按"查询词的稀有度 + 词频饱和 + 长度归一化"排序。

    与 search() 的分工：search() 走语义（向量），本函数走字面（词），
    两者命中集合不同、排序口径也不同 —— 融合是 D17 的事，今天各自独立。

    端到端（入口 → 步骤 → 返回）：
        search_keywords("年假有几天")
          → tokenize(问题)                  查询侧分词（与索引侧同一个函数）
          → build_or_tsquery(tokens)        拼成 "年 | 假 | 有 | 几天"
          → SQL: WHERE content_tsv @@ ...   ② 走 GIN 倒排筛候选
                 + corpus/df 现算全库统计量  ③ 对候选套 BM25 公式排序
          → [{content, source, page_ref, score, retriever}] 按 score 降序

    Args:
        query: 用户问题（原始文本，未分词）
        top_k: 返回条数
    Returns:
        结果列表；**字段与向量检索有意不同**：
        - `score` 而不是 `similarity`：BM25 分数无上界（可 >1），与 cosine 的
          [0,1] 不是一个量纲，混用同一个键名会在 D17 做 RRF 时埋雷
        - `retriever="bm25"`：标明这一路是谁产的，便于调试与消融实验归因
    """
    tokens = tokenize(query)
    if not tokens:
        # 全是标点/空白的问题，清洗后没有任何有效 token。
        # 必须提前返回：空 tsquery 虽然不会报错（@@ 结果为 False），但白跑一次数据库。
        logger.info("BM25 检索跳过：query=%r 清洗后无有效 token", query[:20])
        return []

    async with async_session_factory() as session:
        rows = (await session.execute(
            _BM25_SQL,
            {
                "terms": " ".join(tokens),
                "tsq": build_or_tsquery(tokens),
                "k1": BM25_K1,
                "b": BM25_B,
                "top_k": top_k,
            },
        )).all()

    results = [
        {
            # D17：与向量路用同一个键名、同一种类型（str），融合时才能直接对齐
            "chunk_id": str(row.id),
            "content": row.content,
            # 与向量检索保持同一个键名 "source"：工具层与引用收集器按它取展示名
            "source": row.filename,
            "page_ref": row.page_ref,
            "score": round(float(row.score), 4),
            "retriever": "bm25",
        }
        for row in rows
    ]

    candidate_count = rows[0].candidate_count if rows else 0
    logger.info(
        "BM25 检索完成 query=%r terms=%s 候选=%d 返回=%d",
        query[:20], tokens, candidate_count, len(results),
    )
    return results


# ============================================================
# D17：RRF 混合检索（把上面两条腿合成一个排序）
# ============================================================

# RRF 里的常数 k。出处：Cormack / Clarke / Buettcher,
# "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods",
# SIGIR 2009 —— k=60 是该论文的经验取值，Elasticsearch 等实现沿用。
# ⚠ 诚实标注：**没有为我们的语料调过参**（与 D16 的 b=0.75 同性质），
#   真正调参要等 D20-D24 的评测体系。
RRF_K = 60

# 每路取多少条进融合。PRD §9.3：两路各 top20 → RRF 融合 → top20 →（D19）重排 top5。
# 取 20 而不是默认 3：融合的价值在"给两路的名次做仲裁"，
# 候选窗口太窄会把"单路第 8 名但另一路也认可"的文档直接砍掉 —— 而它恰恰是 RRF 想捞的。
HYBRID_CANDIDATE_K = 20


def fuse_rrf(
    vector_hits: list[dict],
    keyword_hits: list[dict],
    top_k: int = DEFAULT_TOP_K,
    k: int = RRF_K,
) -> list[dict]:
    """
    RRF 融合（纯函数：不碰数据库、不发网络请求，可单独测试）。

        score(d) = Σ 1 / (k + rank_i(d))          rank 从 1 开始

    为什么不是"加权求和分数"（α × cosine + (1-α) × BM25）：
      ① 量纲不同 —— cosine ∈ [0,1]，BM25 无上界（本语料实测 2~3，换语料能到 10+），
         直接相加时 BM25 那一项完全主导，α 变成摆设；
      ② 分布随查询漂移 —— BM25 的 IDF 依赖全库 df，语料一变所有分数都变。
         想用一个固定 α 摆平，等于假设"所有查询的分数分布一样"，而它必然不一样。
      RRF 把**分数整个丢掉、只留名次**，于是两个问题同时消失：
      名次是序数量，天然同量纲；名次也不受分数分布影响。

    机制上它奖励的是"**被多路共同认可**"，不是"单路分数特别高"：
      文档 A：向量 rank1、BM25 缺席 → 1/(60+1)        = 0.016393
      文档 B：向量 rank5、BM25 rank5 → 2/(60+5)        = 0.030769   ← B 赢，且赢近一倍
    代价（双刃剑）：真有文档只在单路相关时，会被"两路都中等"的压过去。
    净收益要靠 D20-D24 的评测量化，本函数不做任何承诺。

    Args:
        vector_hits: search() 的返回（需带 chunk_id）
        keyword_hits: search_keywords() 的返回（需带 chunk_id）
        top_k: 融合后返回条数
        k: RRF 常数，默认 RRF_K=60
    Returns:
        融合后的有序列表（按 rrf_score 降序；**平局按 sort_key 的五档规则决胜**，
        其中最典型的一种平局是"两条文档在两路里名次交换"，分数会精确相等）。
        每个元素除内容外还带：
        - rrf_score                   融合分
        - vector_rank / bm25_rank     该片在两路里的名次（未命中为 None）
        - similarity / bm25_score      两路的**原始分**，只作展示与排查，不参与排序
    """
    merged: dict[str, dict] = {}

    def absorb(hits: list[dict], leg: str) -> None:
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit.get("chunk_id")
            if not chunk_id:
                # 没有身份的结果无法参与融合。正常路径不会发生（两路的 SQL 都取了 id），
                # 这里跳过而不是抛错：宁可少一条，也不要因为一条脏数据让整个检索 500。
                logger.warning("融合跳过无 chunk_id 的结果 leg=%s rank=%d", leg, rank)
                continue

            # setdefault：同一个 chunk_id 第二次出现时**复用**同一份 dict，
            # 于是两次贡献累加到同一个 rrf_score 上，内容字段也用先到的那份（两路内容本就相同）。
            entry = merged.setdefault(
                chunk_id,
                {
                    "chunk_id": chunk_id,
                    "content": hit.get("content"),
                    "source": hit.get("source"),
                    "page_ref": hit.get("page_ref"),
                    "rrf_score": 0.0,
                    "vector_rank": None,
                    "bm25_rank": None,
                    "similarity": None,
                    "bm25_score": None,
                },
            )
            entry["rrf_score"] += 1.0 / (k + rank)

            if leg == "vector":
                entry["vector_rank"] = rank
                entry["similarity"] = hit.get("similarity")
            else:
                entry["bm25_rank"] = rank
                entry["bm25_score"] = hit.get("score")

    absorb(vector_hits, "vector")
    absorb(keyword_hits, "bm25")

    def sort_key(entry: dict) -> tuple:
        """
        五档排序键（前面相同才看后一档）。

        ⚠ 为什么需要这么多档：**RRF 的平局一点都不罕见，而且是结构性的**。
          分数是 1/(k+a) + 1/(k+b)，只要两条文档的名次在两路里是**交换的**
          （一条 v1/b2、另一条 v2/b1），和就精确相等。
          实测：本项目语料查「年假有几天」时 policy=(v2,b1) 与 hr=(v1,b2)
          同为 1/62+1/61 = 0.032522 —— 按文件里原先的兜底（chunk_id），
          先后就交给了 UUID 的运气，而 UUID 是没有语义的。

        各档职责：
          ① rrf_score 降序        —— RRF 主判据
          ② 被几条列表提名（多者优先）—— "两路都认可"胜过"只有一路认可"
             例：向量 rank1 单路得 1/61；两路都 rank1 得 2/61 > 1/61
          ③ 最好名次升序          —— 同为两路提名时，名次更靠前者优先
          ④ 向量名次升序（无则排后）—— ← **这档是策略，不是实测结论**：
             融合分与名次都平手时（典型场景就是上面那个"交换名次"），
             选择相信**语义路**。理由：本项目检索的主路是向量（D9-D12 先建、PRD 的
             hybrid 定义也是"向量为主 + BM25 补字面"），且向量路对改写/近义查询更稳。
             这不是调参调出来的结论，等 D20-D24 有评测集后应回头验证该策略。
          ⑤ chunk_id 升序         —— 终极兜底，只为"同一份数据两次跑出同一顺序"
        """
        ranks = [r for r in (entry["vector_rank"], entry["bm25_rank"]) if r is not None]
        return (
            -entry["rrf_score"],
            -len(ranks),
            min(ranks),
            entry["vector_rank"] if entry["vector_rank"] is not None else 10**9,
            entry["chunk_id"],
        )

    ordered = sorted(merged.values(), key=sort_key)
    for entry in ordered:
        entry["rrf_score"] = round(entry["rrf_score"], 6)
        entry["retriever"] = "hybrid"
    return ordered[:top_k]


async def hybrid_search(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """
    混合检索：向量 + BM25 → RRF 融合。

    端到端（入口 → 步骤 → 返回）：
        hybrid_search("灰度回滚")
          → asyncio.gather( search(20), search_keywords(20) )   两路**并发**发起
          → fuse_rrf(两路结果)                                   只按名次融合
          → [{... rrf_score, vector_rank, bm25_rank ...}]        按 rrf_score 降序

    为什么并发（asyncio.gather）而不是顺序 await：
      两路互不依赖（一路是"embedding 网络调用 + 向量排序"，另一路是"jieba 切词 + 倒排 + BM25 自算"），
      顺序执行的总耗时是两者之和，并发是两者最大值。各拿各的 session，不共享连接。

    Args:
        query: 用户问题（原始文本）
        top_k: 融合后返回条数
    Returns:
        融合结果列表；两路都空时返回 []。
    """
    # 两路各自 top20 进融合（见 HYBRID_CANDIDATE_K 的注释）
    vector_hits, keyword_hits = await asyncio.gather(
        search(query, top_k=HYBRID_CANDIDATE_K),
        search_keywords(query, top_k=HYBRID_CANDIDATE_K),
    )

    fused = fuse_rrf(vector_hits, keyword_hits, top_k=top_k)

    overlap = len({h["chunk_id"] for h in vector_hits} & {h["chunk_id"] for h in keyword_hits})
    logger.info(
        "混合检索完成 query=%r 向量=%d BM25=%d 交集=%d → 融合返回=%d",
        query[:20], len(vector_hits), len(keyword_hits), overlap, len(fused),
    )
    if not keyword_hits:
        # 常见于"查询全是标点/符号"（清洗后无 token）。此时融合自动退化成向量单路，
        # 结果仍可用 —— 只是失去了关键词那一路的仲裁。
        logger.info("混合检索降级：BM25 路无结果，本次等效纯向量检索 query=%r", query[:20])
    return fused
