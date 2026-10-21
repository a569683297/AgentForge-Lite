"""
检索检查器服务（`app/services/retriever_service.py`）的名次口径与 gold 判定
=============================================================================
这页最容易错、而且错了**不会报错**的，就是三个名次混用：

  · `pool_rank`   候选池名次（融合序）
  · `rerank_rank` 重排后完整名次（"被挤到第 12"）
  · `final_rank`  最终 top-k 名次（只有前 k 条有）

一旦把 `rerank_rank` 当成 `final_rank`，页面上"最终 top-5"会显示 20 行 ——
看起来像功能更全了，实际是把"取舍"这件事画没了。这些用例把它钉住。

gold 侧钉的是 D23 定下的**文档级去重**：一篇文档切出几十片，
同一篇命中的多个切片只能记它**最早**的那个名次。不去重会让 Hit@k 虚高。
"""

from __future__ import annotations

from typing import get_args

import pytest

from app.config import RETRIEVER_CONFIGS
from app.schemas.retriever import MAX_TOP_K, RetrieverConfig, RetrieverSearchResponse
from app.services import retriever_service as svc


# ---------------------------------------------------------------- 常量一致性


def test_config_literal_matches_source_of_truth() -> None:
    """
    `RetrieverConfig` 是手写的 Literal（Literal 不吃动态展开），
    必须与 `config.RETRIEVER_CONFIGS` 一致。不一致的症状很隐蔽：
    后端加了配置、schema 没加 → 那个配置**永远选不到**，但没有任何报错。
    """
    assert set(get_args(RetrieverConfig)) == set(RETRIEVER_CONFIGS)


def test_max_top_k_equals_candidate_pool() -> None:
    """
    top_k 的上限必须等于候选池宽度。大于它，右列会比左列还长，
    那就不是「从 20 条里挑几 条」，而是凭空变出了候选。
    """
    assert MAX_TOP_K == svc.CANDIDATE_K


# ---------------------------------------------------------------- 假数据


def _hit(index: int, document_id: str) -> dict:
    """造一条融合后的候选（字段形状与 `fuse_rrf()` 的输出一致）。"""
    return {
        "chunk_id": str(index),
        "content": f"片段 {index} 的正文",
        "source": f"doc-{document_id}.md",
        "page_ref": None,
        "similarity": round(1 - index * 0.01, 4),
        "rrf_score": round(1 / (60 + index), 6),
        "vector_rank": index,
        "bm25_rank": None if index % 2 else index,
    }


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hits: list[dict],
    gold: dict | None,
    doc_map: dict[str, str] | None = None,
) -> None:
    """把三条外部依赖全换成假实现：检索、重排、文档映射 + gold。"""

    async def fake_hybrid(query: str, top_k: int, **kwargs) -> list[dict]:
        return hits[:top_k]

    async def fake_rerank(
        query: str, candidates: list[dict], top_n: int, return_all: bool = False
    ) -> tuple[list[dict], bool]:
        # 反转顺序：模拟"融合第 1 名被重排压到最后"，这样名次变化必然非零
        ranked = [
            {**item, "rerank_score": round(10.0 - i, 3)}
            for i, item in enumerate(reversed(candidates), start=1)
        ]
        return (ranked if return_all else ranked[:top_n]), True

    async def fake_doc_map(chunk_ids: list[str]) -> dict[str, str]:
        if doc_map is not None:
            return {cid: doc_map.get(cid, f"d{cid}") for cid in chunk_ids}
        return {cid: f"d{cid}" for cid in chunk_ids}

    async def fake_gold(case_key: str | None) -> dict | None:
        return gold

    monkeypatch.setattr(svc, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(svc, "rerank", fake_rerank)
    monkeypatch.setattr(svc, "_chunk_doc_map", fake_doc_map)
    monkeypatch.setattr(svc, "_load_gold", fake_gold)


GOLD_ONE_DOC = {
    "case_key": "A01",
    "question": "年假有几天？",
    "category": "doc_qa",
    "doc_slugs": ["annual-leave"],
    "doc_ids": {"d1", "d2"},
    "is_negative": False,
}


# ---------------------------------------------------------------- 名次口径


async def test_final_rank_only_covers_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    最核心的一条：`final_rank` 只有 top_k 条，`rerank_rank` 是全部 20 条。

    这两个字段混用的后果不是报错，而是右列悄悄变成 20 行 ——
    页面看上去更"完整"，但"从 20 里挑 5"这个过程就再也看不出来了。
    """
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]
    _patch(monkeypatch, hits=hits, gold=None)

    result = await svc.search_debug("年假有几天？", top_k=5, config="hybrid_rerank")

    assert len(result["candidates"]) == 20
    assert sum(1 for row in result["candidates"] if row["final_rank"] is not None) == 5
    # 重排名次是完整的一套 1..20
    assert {row["rerank_rank"] for row in result["candidates"]} == set(range(1, 21))


async def test_rank_flip_is_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """融合第 1 名被重排压到第 20 名时，页面必须能读到这两个数字。"""
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]
    _patch(monkeypatch, hits=hits, gold=None)

    result = await svc.search_debug("q", top_k=5, config="hybrid_rerank")
    by_chunk = {row["chunk_id"]: row for row in result["candidates"]}

    last_pool = by_chunk["1"]          # 融合第 1 名
    assert last_pool["rerank_rank"] == 20
    assert last_pool["final_rank"] is None      # 被淘汰

    first_pool = by_chunk["20"]        # 融合第 20 名
    assert first_pool["rerank_rank"] == 1
    assert first_pool["final_rank"] == 1


async def test_schema_accepts_service_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """service 返回的 dict 必须能被响应模型直接接住（接口层就是这么用的）。"""
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]
    _patch(monkeypatch, hits=hits, gold=GOLD_ONE_DOC)

    result = await svc.search_debug("q", top_k=5, case_key="A01")
    model = RetrieverSearchResponse(**result)

    assert model.effective_config == "hybrid_rerank"
    assert model.score_kind == "rerank_logit"
    assert len(model.candidates) == 20
    assert model.gold is not None


# ---------------------------------------------------------------- gold 判定


async def test_gold_dedupes_by_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    同一篇 gold 文档的多个切片同时进最终结果时，`hit_count` 按**文档**算。

    构造：重排把候选反转（chunk 20 第 1、19 第 2、18 第 3 …），
    再把 chunk 20 与 19 都映射到 d1、chunk 18 映射到 d2 ——
    即"d1 的两个切片占了第 1、2 名，d2 在第 3 名"。

    这正是 D23 那条纪律的现场：两片都算的话 hit_count 会变成 3，
    Hit@k 分母明明只有 2 篇，却"命中 3 篇"。去重后应是 2 篇、最早第 1 名。
    """
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]
    doc_map = {"20": "d1", "19": "d1", "18": "d2"}
    _patch(monkeypatch, hits=hits, gold=GOLD_ONE_DOC, doc_map=doc_map)

    result = await svc.search_debug("q", top_k=5, case_key="A01")
    gold = result["gold"]

    assert gold is not None
    assert gold["has_gold"] is True
    assert gold["expected_count"] == 2
    # 三片命中、两篇文档 —— 去重后是 2
    assert gold["hit_count"] == 2
    assert gold["hit_rank"] == 1        # d1 的最早名次是第 1 名
    assert gold["mrr"] == pytest.approx(1.0, rel=1e-6)
    assert gold["hit"] is True


async def test_gold_miss_reports_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """一篇都没命中时 hit=False、mrr=0，而不是缺字段（前端要读这两个值）。"""
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]
    gold = {**GOLD_ONE_DOC, "doc_ids": {"d999"}}
    _patch(monkeypatch, hits=hits, gold=gold)

    result = await svc.search_debug("q", top_k=5, case_key="A01")

    assert result["gold"]["hit"] is False
    assert result["gold"]["hit_rank"] is None
    assert result["gold"]["mrr"] == 0.0


async def test_no_case_key_leaves_gold_undetermined(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    没选题时 `is_gold` 必须是 None（三态），不能是 False。

    False 会让页面画出 20 个灰点，读起来是"全都没命中"；
    真相是"根本没有标准答案可比" —— 这是自由查询的常态，不是失败。
    """
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]
    _patch(monkeypatch, hits=hits, gold=None)

    result = await svc.search_debug("q", top_k=5)

    assert result["gold"] is None
    assert all(row["is_gold"] is None for row in result["candidates"])


# ---------------------------------------------------------------- 边界


async def test_unknown_config_raises_value_error() -> None:
    """显式传参这条路 pydantic 拦不住（脚本/调试会直接调 service）。"""
    with pytest.raises(ValueError):
        await svc.search_debug("q", config="not_a_config")


async def test_unknown_case_key_raises_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """题号不存在要能翻译成 404，而不是静默当成"没有 gold"。"""
    hits = [_hit(i, f"d{i}") for i in range(1, 21)]

    async def fake_gold(case_key: str | None) -> dict | None:
        raise LookupError(case_key)

    _patch(monkeypatch, hits=hits, gold=None)
    monkeypatch.setattr(svc, "_load_gold", fake_gold)

    with pytest.raises(LookupError):
        await svc.search_debug("q", case_key="ZZ99")


async def test_empty_chunk_ids_short_circuit() -> None:
    """空列表要提前返回：`= any('{}')` 在 PG 上会因推断不出数组类型而报错。"""
    assert await svc._chunk_doc_map([]) == {}


async def test_no_gold_without_case_key() -> None:
    """`_load_gold(None)` 不发查询 —— 自由查询不该为 gold 付一次 DB 往返。"""
    assert await svc._load_gold(None) is None
