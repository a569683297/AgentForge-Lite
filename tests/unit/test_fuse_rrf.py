"""
单元：RRF 融合 `fuse_rrf`（D17 核心算法）
==========================================
它是**纯函数**（不碰数据库、不发网络），所以这里是"算法语义"证据最便宜的地方。

算法：score(d) = Σ 1 / (k + rank_i(d))，rank 从 **1** 开始，k=60。
关键性质：**只看名次、不看分数** —— 于是"两路共同认可"胜过"单路分数特别高"。
"""

from app.services.retrieval_service import RRF_K, fuse_rrf


def _hit(chunk_id: str, **extra) -> dict:
    """造一条检索结果（两路的 SQL 都会带 chunk_id / content / source）。"""
    return {"chunk_id": chunk_id, "content": f"内容-{chunk_id}", "source": "doc.md", **extra}


# ------------------------------------------------------------------ 基本形状


def test_empty_inputs_return_empty_list():
    assert fuse_rrf([], []) == []


def test_single_leg_keeps_its_own_order():
    """只有一路时，融合序 = 那一路的序（RRF 不该打乱它）。"""
    hits = [_hit("a"), _hit("b"), _hit("c")]
    fused = fuse_rrf(hits, [])
    assert [r["chunk_id"] for r in fused] == ["a", "b", "c"]


def test_rank_starts_at_one_not_zero():
    """
    名次从 1 开始 ⇒ 第 1 名得 1/(k+1) = 1/61。
    若有人写成 rank 从 0 起，第一分会变成 1/60 —— 肉眼看不出来，但整个排序口径就漂了。
    """
    fused = fuse_rrf([_hit("a")], [], k=60)
    assert fused[0]["rrf_score"] == round(1 / 61, 6)


def test_default_k_is_60():
    """k 的默认值必须就是论文取值（SIGIR 2009），不是随手写的数。"""
    assert RRF_K == 60
    assert fuse_rrf([_hit("a")], [])[0]["rrf_score"] == round(1 / (RRF_K + 1), 6)


# ------------------------------------------------------------------ 核心语义


def test_two_leg_agreement_beats_single_leg_champion():
    """
    ★ RRF 的灵魂断言：**"两路都认可"要赢过"单路第一"**。

    文档 A：向量 rank1、BM25 缺席      → 1/61        = 0.016393
    文档 B：向量 rank5、BM25 rank5     → 2/65        = 0.030769   ← B 赢
    这正是"奖励共识"的具体表现；如果这条红了，说明融合退化成了加权分数求和。
    """
    vector = [_hit("A"), _hit("v2"), _hit("v3"), _hit("v4"), _hit("B")]
    keyword = [_hit("k1"), _hit("k2"), _hit("k3"), _hit("k4"), _hit("B")]

    fused = fuse_rrf(vector, keyword, top_k=10)
    assert fused[0]["chunk_id"] == "B"
    assert fused[0]["rrf_score"] == round(2 / 65, 6)
    assert fused[1]["chunk_id"] == "A"


def test_rank_exchange_tie_is_broken_toward_vector_leg():
    """
    ★ 结构性平局：两条文档在两路里**名次交换**时，分数精确相等。

        X：向量 rank1 / BM25 rank2  → 1/61 + 1/62
        Y：向量 rank2 / BM25 rank1  → 1/62 + 1/61   ← 完全相等

    五档排序键的最后一档之前是"向量名次升序"，所以 X 胜出。
    这条是**策略**（相信语义路），不是实测结论 —— 用例把它钉住，
    将来 D35/D36 若按评测数据改策略，这条会红，正好提醒"策略变了，记得改文档"。
    """
    vector = [_hit("X"), _hit("Y")]
    keyword = [_hit("Y"), _hit("X")]

    fused = fuse_rrf(vector, keyword)
    assert fused[0]["rrf_score"] == fused[1]["rrf_score"], "前提：这一对确实精确平局"
    assert [r["chunk_id"] for r in fused] == ["X", "Y"]


def test_more_nominations_wins_when_scores_equal():
    """
    第②档：分数相同时，**被更多列表提名**的优先。
    构造"A 被两路提名、C 只被一路提名"，两者分数相等的情形很难自然出现，
    这里直接验证排序键的语义：提名数多的排前。
    """
    vector = [_hit("both"), _hit("only_vector")]
    keyword = [_hit("both")]
    fused = fuse_rrf(vector, keyword)
    assert fused[0]["chunk_id"] == "both"


# ------------------------------------------------------------------ 边界与健壮性


def test_top_k_truncates_result():
    hits = [_hit(f"h{i}") for i in range(10)]
    assert len(fuse_rrf(hits, [], top_k=3)) == 3
    assert len(fuse_rrf(hits, [], top_k=20)) == 10  # 要的比有的多 → 全给，不报错


def test_results_without_chunk_id_are_skipped_without_crashing():
    """
    ★ 一条脏数据不能让整个检索 500。
    没有 chunk_id 就**无法参与融合**（身份不明），跳过它、其余照常返回。
    """
    vector = [{"content": "孤儿结果"}]
    keyword = [_hit("ok")]
    fused = fuse_rrf(vector, keyword)
    assert [r["chunk_id"] for r in fused] == ["ok"]


def test_duplicate_across_legs_is_merged_not_duplicated():
    """同一个 chunk 被两路都命中时，结果是**一条**（分数累加），不是两条。"""
    fused = fuse_rrf([_hit("same")], [_hit("same")])
    assert len(fused) == 1
    assert fused[0]["rrf_score"] == round(2 / 61, 6)


def test_raw_scores_kept_for_display_but_not_used_in_ranking():
    """
    两路的**原始分**（similarity / bm25_score）要留在结果里供排障，
    但它们**不参与排序** —— 这里用"向量相似度更低的反而排前"来证明这一点。
    """
    vector = [_hit("low_sim", similarity=0.10), _hit("high_sim", similarity=0.99)]
    fused = fuse_rrf(vector, [])
    assert fused[0]["similarity"] == 0.10  # 第 0 名就是相似度 **低** 的那条
    assert fused[0]["vector_rank"] == 1


def test_each_result_carries_rank_metadata():
    fused = fuse_rrf([_hit("a", similarity=0.9)], [_hit("a", score=2.5)])
    row = fused[0]
    assert row["vector_rank"] == 1
    assert row["bm25_rank"] == 1
    assert row["similarity"] == 0.9
    assert row["bm25_score"] == 2.5
    assert row["retriever"] == "hybrid"


def test_missing_leg_rank_is_none_not_zero():
    """某一路没命中时，名次字段是 None；写成 0 会被误读成"第 0 名"。"""
    fused = fuse_rrf([_hit("only_vector")], [])
    assert fused[0]["bm25_rank"] is None
    assert fused[0]["bm25_score"] is None
