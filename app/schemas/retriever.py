"""
检索检查器（RetrieverPage）的请求 / 响应模型。

字段命名的两条约定，都是为了让前端**不必猜**：

1. `pool_rank` 与 `final_rank` 分开，不共用一个 `rank`：
   同一条片段在候选池里是第 7 名、重排后可能第 1 名。只给一个 `rank`，
   页面的两列就没法各自排序，最后只能在前端靠数组下标凑 —— 一改后端顺序就错。
2. 四种分数量纲**各自独立成字段**（`similarity` / `bm25_score` / `rrf_score` / `rerank_score`），
   不合并成一个 `score`。它们不可比：余弦 ∈ [0,1]、BM25 无上界、
   RRF 是 1/(60+名次) 累加（0.016~0.033）、logit 跨 query 也不可比。
   页面用一个 `score` 字段展示，必然会出现"分数从 1.92 变成 0.0249"这种
   看起来像坏了、其实是换了量纲的情况。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ⚠ 与 `app/config.py::RETRIEVER_CONFIGS` 必须保持一致。
# 为什么不用 `Literal[*RETRIEVER_CONFIGS]` 动态生成：Literal 只接受字面量，
# 拿 tuple 展开会变成"运行期才报错"的类型。代价是两处要同步 ——
# `tests/unit/test_retriever_service.py` 里有一条断言把这两个来源钉在一起，
# 改了配置却忘了改这里会直接红。
RetrieverConfig = Literal["pure_vector", "hybrid", "hybrid_rerank"]

# 候选池宽度上限，同时也是 top_k 的上限（见 CANDIDATE_K）。
MAX_TOP_K = 20


class RetrieverSearchRequest(BaseModel):
    """一次检索检查请求。"""

    query: str = Field(min_length=1, max_length=500, description="检索问题原文")
    top_k: int = Field(
        default=5,
        ge=1,
        le=MAX_TOP_K,
        description=f"最终保留条数（1..{MAX_TOP_K}）。上限就是候选池宽度 —— "
        "超过它，右列会比左列还长，那就不是「取舍」了",
    )
    config: RetrieverConfig | None = Field(
        default=None,
        description="检索配置。为空时用服务端默认（settings.retriever_config）",
    )
    case_key: str | None = Field(
        default=None,
        max_length=32,
        description="评测题号（如 A01）。给了才判定 gold 并算 hit / MRR；"
        "不给则 is_gold 为 null（自由查询没有标准答案文档）",
    )


class RetrieverCandidateOut(BaseModel):
    """
    候选池里的一条片段 —— 左列的一行，也是右列的数据来源。

    同一条记录同时服务两列：左列按 `pool_rank` 排、显示 `score`；
    右列筛出 `final_rank != null` 的行、按 `final_rank` 排、
    显示 `rerank_score`（没有则退回 `score`）。**不返回两个数组**，
    否则前端无法知道"左列第 7 行"和"右列第 1 行"是同一条。
    """

    pool_rank: int = Field(description="候选池名次（融合序 / 相似度序）")
    chunk_id: str = Field(description="切片主键；两列对齐的唯一依据")
    doc_id: str | None = Field(description="所属文档 UUID；gold 判定落在它上面")
    source: str | None = Field(description="来源文档名")
    page_ref: str | None = Field(default=None, description="页码（PDF 类才有）")
    content: str = Field(description="片段正文")

    score: float | None = Field(
        description="候选池阶段的主分数：similarity（纯向量）或 rrf_score（融合）"
    )
    similarity: float | None = Field(default=None, description="余弦相似度，仅向量路有")
    bm25_score: float | None = Field(default=None, description="BM25 分，无上界，仅关键词路有")
    rrf_score: float | None = Field(default=None, description="RRF 融合分，仅融合路径有")
    vector_rank: int | None = Field(default=None, description="该片在向量路里的名次")
    bm25_rank: int | None = Field(default=None, description="该片在 BM25 路里的名次")

    rerank_rank: int | None = Field(
        default=None,
        description="重排后的**完整**名次（1..候选池宽度）。null = 本次没有跑重排。"
        "它和 final_rank 的区别就是「掉到第 12」与「没进前 5」的区别 —— "
        "调参时想知道的是前者",
    )
    final_rank: int | None = Field(
        default=None,
        description="最终 top-k 里的名次；null = 没进最终结果（被取舍掉了）",
    )
    rerank_score: float | None = Field(
        default=None,
        description="重排 logit。**只有真跑过重排才有**；降级与非重排配置为 null",
    )
    is_gold: bool | None = Field(
        default=None,
        description="是否属于该题的 gold 文档。null = 本次查询没有 gold（未选题 / 该题无依据文档）",
    )


class RetrieverGoldOut(BaseModel):
    """一道评测题的判定结果（文档级去重口径，与 D23 一致）。"""

    case_key: str
    question: str
    category: str
    is_negative: bool = Field(description="负例：正确答案是「知识库里没有」，按定义无 gold")
    doc_slugs: list[str] = Field(description="期望命中的文档业务名（annual-leave 等）")
    has_gold: bool = Field(description="该题有没有依据文档。负例与工具题为 false")
    expected_count: int = Field(description="gold 文档篇数（分母）")
    hit_count: int = Field(description="最终 top-k 里命中的 gold 文档篇数（已去重）")
    hit: bool = Field(description="Hit@k：至少命中一篇")
    hit_rank: int | None = Field(description="最早命中的文档名次，用于算 MRR")
    mrr: float = Field(description="1 / hit_rank；未命中为 0")


class RetrieverOptionsOut(BaseModel):
    """
    页面初始化用的可选项 —— 由后端说出"有哪些配置、k 能填多大"。

    存在的理由：这三样各自都有硬约束的另一端（配置名对 `RETRIEVER_CONFIGS`、
    `max_top_k` 对候选池宽度、默认值对 `settings`）。前端硬编码一份，
    后端改一处，页面就会拿不存在的配置名去请求。
    """

    configs: list[str] = Field(description="可选检索配置")
    default_config: str = Field(description="服务端默认配置（settings.retriever_config）")
    max_top_k: int = Field(description="top_k 上限 = 候选池宽度")
    default_top_k: int = Field(description="页面初始的 top-k")


class RetrieverTimingsOut(BaseModel):
    """
    分段耗时。`total_ms` 是墙钟总时长，**包含**下面三段，不能相加。

    为什么单独给 `rerank_ms`：它是整条链路里最贵的一段（20 候选 ≈ 750~820ms，D18 实测），
    也是唯一会降级的一段。页面要能一眼看出"慢在重排"还是"慢在检索"。
    """

    retrieve_ms: float = Field(description="检索（向量+BM25 并发）耗时；纯向量时同样计入")
    rerank_ms: float = Field(description="重排耗时；没跑重排为 0")
    resolve_ms: float = Field(description="补文档身份 + 查 gold 的耗时（不在检索链路里）")
    total_ms: float = Field(description="整个请求的墙钟耗时（≈ 上面三段之和 + 组装开销）")


class RetrieverSearchResponse(BaseModel):
    """一次检索检查的完整结果。"""

    query: str
    requested_config: str = Field(description="请求里指定的配置")
    effective_config: str = Field(
        description="**实际走通**的路径。重排降级时是 hybrid，不是 hybrid_rerank —— "
        "与 D19「降级可归因」同一条原则"
    )
    reranked: bool = Field(description="是否真的跑了重排")
    degrade_reason: str | None = Field(
        default=None, description="降级原因（人话）。非 null 时右列分数不是 logit"
    )
    score_kind: Literal["similarity", "rrf_score", "rerank_logit"] = Field(
        description="主分数口径。`rerank_logit` 表示重排生效；其余两种时右列沿用左列分数"
    )
    top_k: int
    candidate_k: int = Field(description="候选池宽度（左列条数，通常 20）")

    timings: RetrieverTimingsOut
    candidates: list[RetrieverCandidateOut] = Field(
        description="候选池，按 pool_rank 升序。前端按 final_rank 筛出右列"
    )
    gold: RetrieverGoldOut | None = Field(
        default=None, description="未指定 case_key 时为 null"
    )
