"""
检索检查器（RetrieverPage）后端：把一次检索的**取舍过程**摊开给页面看。

它和 `retrieval_service.retrieve()` 是**同一条链路的两个出口**：

  · `retrieve()`     生产出口 —— 只给最终 top_k。上层是 Agent 与评测，
                     它们不需要"第 12 名长什么样"，多给 15 条正文纯属浪费。
  · `search_debug()` 调试出口 —— 给「候选池 20 条 + 融合名次 + 重排名次 + gold 判定」。
                     页面要回答的是"为什么是这 5 条、凭什么"，缺任何一段都答不了。

为什么不在 `retrieve()` 上加参数、让它一次全返回：
  它是 Agent 与评测共用的入口，返回体每多一个键，两处消费方都要跟着变；
  而这里要的东西（20 条正文、逐段耗时、gold 比对）对它们全是负担。

为什么不复用 `SPAN_RETRIEVAL`：见 `observability.SPAN_INSPECT` 的注释 ——
手工检索会被反复点击调参，混进真实链路会让 P95 失真。

**gold 的使用纪律**（D23 定下的，这里同样遵守）：
  gold 只用于**事后比对**，绝不参与检索。一旦拿它当过滤条件
  （"只在这两篇里搜"），等于开卷考试还给了页码，检索难度归零。
  所以这里的顺序是：先跑完整检索 → 再拿 doc_ids 比对 → 最后算 hit / MRR。
"""

from __future__ import annotations

import uuid

import time

from sqlalchemy import select

from app.config import RETRIEVER_CONFIGS, settings
from app.core.db import async_session_factory
from app.core.logging import logger
from app.models.document_chunk import DocumentChunk
from app.models.eval import EvalCase
from app.services.observability import SPAN_INSPECT, span
from app.services.rerank_service import rerank
from app.services.retrieval_service import (
    DEFAULT_TOP_K,
    HYBRID_CANDIDATE_K,
    RERANK_CANDIDATE_K,
    hybrid_search,
    search,
)

# 候选池宽度 = 融合窗口。三种配置取同一个数，左列才可比：
# 换成 pure_vector 时如果只取 5 条，页面会呈现"候选池突然变窄"的假象，
# 而真相是"向量路本来就能给 20 条，只是没多要"。
CANDIDATE_K = max(HYBRID_CANDIDATE_K, RERANK_CANDIDATE_K)


async def _chunk_doc_map(chunk_ids: list[str]) -> dict[str, str]:
    """
    chunk_id → document_id（字符串形式的 UUID）。

    为什么需要这一步：两路检索的 SQL 都只带 `Document.filename`（展示用），
    没有 `document_id`。而 gold 判定必须落在**文档身份**上 ——
    同一篇文档切出几十片，靠文件名比对在遇到过同名文件时会静默错配。

    为什么不直接改 `search()` 的 SQL 多 select 一列：
    那是 Agent 与评测的主链路。为了一个调试页面去动它，收益是省一次查询，
    代价是两处消费方都要重新验证。批量查一次（312 片规模 < 5ms）更划算。

    空列表提前返回：`= any('{}')` 在 PostgreSQL 上会因无法推断数组类型而报错，
    不是"空集合恒假"那么无害（D23 踩过）。
    """
    ids: list[int] = []
    for raw in chunk_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(DocumentChunk.id, DocumentChunk.document_id).where(
                    DocumentChunk.id.in_(ids)
                )
            )
        ).all()
    return {str(row.id): str(row.document_id) for row in rows}


async def _load_gold(case_key: str | None) -> dict | None:
    """
    取一道评测题的 gold（标准答案文档集合）；`case_key` 为空返回 None。

    `doc_ids` 只有 35/50 条题有 —— 负例（正确答案是"库里没有"）与工具题
    （判定依据是"有没有调对工具"）**按定义就没有依据文档**。
    这不是数据缺失，是口径：这两类题的 hit / MRR 算出来必然是 0，
    算进去等于凭空拉低检索指标。所以 `has_gold` 要如实传出去，让页面写清原因。
    """
    if not case_key:
        return None

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(
                    EvalCase.case_key,
                    EvalCase.question,
                    EvalCase.category,
                    EvalCase.doc_ids,
                    EvalCase.doc_slugs,
                    EvalCase.is_negative,
                ).where(EvalCase.case_key == case_key)
            )
        ).first()
    if row is None:
        raise LookupError(case_key)

    return {
        "case_key": row.case_key,
        "question": row.question,
        "category": row.category,
        "doc_slugs": list(row.doc_slugs or []),
        "doc_ids": {str(x) for x in (row.doc_ids or [])},
        "is_negative": bool(row.is_negative),
    }


async def search_debug(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    config: str | None = None,
    case_key: str | None = None,
    *,
    owner_id: uuid.UUID | None = None,
) -> dict:
    """
    跑一次完整检索，并返回"过程 + 结果 + 判定"三部分。

    端到端（入口 → 步骤 → 返回）：
        search_debug("年假有几天", top_k=5, config="hybrid_rerank", case_key="A01")
          → hybrid_search(query, 20)                两路并发 → RRF → 融合序 20 条
          → rerank(query, 融合后20, top_n=5, return_all=True)
                                                    重排 → 完整 20 条的重排名次
          → _chunk_doc_map(20 个 chunk_id)          切片 → 文档
          → _load_gold("A01")                       取 doc_ids
          → 组装：每条的候选池名次 / 最终名次 / 各阶段分数 / 是否命中 gold
          → 汇总 hit（最终 top-k 里有没有 gold）与 MRR（第一个命中的倒数名次）

    Args:
        query: 用户问题
        top_k: 最终保留条数（1..20；上限 = 候选池宽度，超了右列就比左列还长）
        config: 检索配置；None 则读 settings.retriever_config
        case_key: 评测题号；给了才算 gold，不给则 is_gold 为 None

    Returns:
        见 `app/schemas/retriever.py::RetrieverSearchResponse` 的字段说明。

    Raises:
        ValueError: config 非法（显式传参这条路 pydantic 拦不住）
        LookupError: case_key 不存在
    """
    name = (config or settings.retriever_config).strip().lower()
    if name not in RETRIEVER_CONFIGS:
        raise ValueError(f"未知检索配置：{name!r}，可选 {RETRIEVER_CONFIGS}")

    started = time.perf_counter()
    with span(
        SPAN_INSPECT,
        as_type="retriever",
        input={"query": query, "config": name, "top_k": top_k, "case_key": case_key},
    ) as obs:
        reranked = False
        degrade_reason: str | None = None
        retrieve_ms = 0.0
        rerank_ms = 0.0

        # ---------- ① 候选池 ----------
        if name == "pure_vector":
            candidates = await search(query, top_k=CANDIDATE_K, owner_id=owner_id)
            effective = "pure_vector"
            score_kind = "similarity"
            # 没有融合、也没有重排：最终名次就是相似度名次
            final_pos: dict[str, int] = {
                str(hit["chunk_id"]): i for i, hit in enumerate(candidates[:top_k], 1)
            }
            rerank_pos: dict[str, int] = {}
            rerank_scores: dict[str, float] = {}

        else:
            t0 = time.perf_counter()
            candidates = await hybrid_search(
                query, top_k=CANDIDATE_K, owner_id=owner_id
            )
            retrieve_ms = (time.perf_counter() - t0) * 1000
            effective = "hybrid"
            score_kind = "rrf_score"
            rerank_pos: dict[str, int] = {}
            rerank_scores = {}

            if name == "hybrid_rerank" and candidates:
                t0 = time.perf_counter()
                ranked, reranked = await rerank(
                    query, candidates, top_n=top_k, return_all=True
                )
                rerank_ms = (time.perf_counter() - t0) * 1000

                if reranked:
                    effective = "hybrid_rerank"
                    score_kind = "rerank_logit"
                    # 完整名次（1..20）：页面要靠它说"这条被挤到第 12 去了"。
                    # 只取前 top_k 的话，被淘汰的 15 条就只剩"没进去"三个字，
                    # 而"掉了 8 名"才是调参时真正想看的信号。
                    rerank_pos = {
                        str(item["chunk_id"]): i for i, item in enumerate(ranked, 1)
                    }
                    # 最终名次只有前 top_k —— 这是"最终结果"的定义，不能放宽
                    final_pos = {
                        cid: pos for cid, pos in rerank_pos.items() if pos <= top_k
                    }
                    rerank_scores = {
                        str(item["chunk_id"]): item.get("rerank_score") for item in ranked
                    }
                else:
                    # 降级：右列退回融合序前 top_k，并**保留原因**。
                    # 不写 reason 的话，页面看起来"重排跑了"，只是名次恰好没变 ——
                    # 这是最糟的一种误导：用户会以为调参没效果。
                    degrade_reason = (
                        "重排未生效（超时 / 推理异常 / 分数条数不匹配），"
                        "右列退回融合序，分数不是 logit"
                    )
                    final_pos = {
                        str(hit["chunk_id"]): i for i, hit in enumerate(candidates[:top_k], 1)
                    }
            else:
                # hybrid（无重排）+ 空候选两种走同一支：最终名次 = 融合序
                final_pos = {
                    str(hit["chunk_id"]): i for i, hit in enumerate(candidates[:top_k], 1)
                }

        # ---------- ② 文档身份与 gold ----------
        t0 = time.perf_counter()
        doc_map = await _chunk_doc_map([str(hit["chunk_id"]) for hit in candidates])
        gold = await _load_gold(case_key)
        resolve_ms = (time.perf_counter() - t0) * 1000

        gold_ids = gold["doc_ids"] if gold is not None else None

        # ---------- ③ 组装 ----------
        rows: list[dict] = []
        for pool_rank, hit in enumerate(candidates, start=1):
            chunk_id = str(hit["chunk_id"])
            doc_id = doc_map.get(chunk_id)
            rows.append(
                {
                    "pool_rank": pool_rank,
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "source": hit.get("source"),
                    "page_ref": hit.get("page_ref"),
                    "content": hit.get("content") or "",
                    # 左列主分数：候选池阶段的口径，由 score_kind 说明是什么
                    "score": hit.get("similarity")
                    if score_kind == "similarity"
                    else hit.get("rrf_score"),
                    "similarity": hit.get("similarity"),
                    "bm25_score": hit.get("bm25_score"),
                    "rrf_score": hit.get("rrf_score"),
                    "vector_rank": hit.get("vector_rank"),
                    "bm25_rank": hit.get("bm25_rank"),
                    "rerank_rank": rerank_pos.get(chunk_id),
                    "final_rank": final_pos.get(chunk_id),
                    "rerank_score": rerank_scores.get(chunk_id),
                    # gold 为 None 时三态：None = 没选题（无法判定），
                    # False = 有 gold 但这条不是，True = 命中
                    "is_gold": None if gold_ids is None else (doc_id in gold_ids),
                }
            )

        # ---------- ④ gold 判定（文档级去重，与 D23 口径一致）----------
        gold_block: dict | None = None
        if gold is not None:
            final_rows = sorted(
                (row for row in rows if row["final_rank"] is not None),
                key=lambda row: row["final_rank"],
            )
            # 文档级去重：一篇文档的多个切片命中只记它在最终结果里的**最早名次**。
            # 不去重会让 Hit@k 虚高（自己跟自己重复计数），
            # 而且跨文档题的"两篇都捞到"永远为假（第二篇被第一篇自己的切片挤出窗口）。
            best_rank: dict[str, int] = {}
            for row in final_rows:
                if row["is_gold"] and row["doc_id"] not in best_rank:
                    best_rank[row["doc_id"]] = row["final_rank"]

            top_rank = min(best_rank.values()) if best_rank else None
            gold_block = {
                "case_key": gold["case_key"],
                "question": gold["question"],
                "category": gold["category"],
                "is_negative": gold["is_negative"],
                "doc_slugs": gold["doc_slugs"],
                "has_gold": bool(gold["doc_ids"]),
                "expected_count": len(gold["doc_ids"]),
                "hit_count": len(best_rank),
                "hit": bool(best_rank),
                "hit_rank": top_rank,
                "mrr": round(1.0 / top_rank, 4) if top_rank else 0.0,
            }

        total_ms = (time.perf_counter() - started) * 1000
        obs.update(
            output={
                "candidates": len(rows),
                "final": sum(1 for row in rows if row["final_rank"] is not None),
                "reranked": reranked,
                "hit": gold_block["hit"] if gold_block else None,
            }
        )
        logger.info(
            "检索检查 config=%s reranked=%s 候选=%d 最终=%d 耗时=%.0fms query=%r",
            effective,
            reranked,
            len(rows),
            sum(1 for row in rows if row["final_rank"] is not None),
            total_ms,
            query[:20],
        )

        return {
            "query": query,
            "requested_config": name,
            "effective_config": effective,
            "reranked": reranked,
            "degrade_reason": degrade_reason,
            "score_kind": score_kind,
            "top_k": top_k,
            "candidate_k": CANDIDATE_K,
            "timings": {
                "retrieve_ms": round(retrieve_ms, 1),
                "rerank_ms": round(rerank_ms, 1),
                "resolve_ms": round(resolve_ms, 1),
                "total_ms": round(total_ms, 1),
            },
            "candidates": rows,
            "gold": gold_block,
        }
