"""
重排服务（RAG 精排层，D19）
============================
把 D18 讲过的 cross-encoder 真正接进检索链路。

在流水线里的位置（PRD §9.3）：

    向量 top20 ┐
               ├─ RRF 融合 ─→ top20 ─→ **重排** ─→ top5 ─→ 组装 prompt
    BM25  top20 ┘

为什么需要它（D17 的 RRF 留下了一个盲区）：
  RRF **只吃名次、完全看不到内容**。所以它能仲裁"两路意见不一致"，
  却纠正不了"两路一致但都错了"——两边都把某条排前面时，RRF 只会顺着它。
  cross-encoder 把 `[query, 文档]` 拼成一条序列逐层交叉，能读出内容层面的相关性，
  这是它唯一能干、而前面所有步骤都干不了的事。

代价（D18 实测，必须记住）：
  - 20 条候选 @300 字/片 ≈ 750~820ms（load≤10）——链路里最大的一块串行成本
  - 它是 **CPU 密集的同步计算**，不是"等外部" → 必须丢线程，否则占死事件循环
  - 不能建索引、不能缓存结果（同一篇文档换个 query 分数就变）

降级设计（本模块的核心逻辑）：
  模型加载失败 / 推理超时 / 推理抛异常 → **不让请求 500**，改为返回融合序的前 N 条。
  降级**必须可归因**：调用方靠返回值里的 `reranked` 标志区分，
  再由 retrieval_service 把 `retriever` 标成 "hybrid"（而不是 "hybrid_rerank"）。
  否则评测时降级样本被算进 hybrid_rerank 的成绩，C 会被拉向 B，
  得出"重排没用甚至有害"的错误结论 —— 而代码不会有任何报错。

⚠ 首次加载若模型未缓存需要联网下载（约 1GB）：
  国内 HF 直连不通，需先导出 `HF_ENDPOINT=https://hf-mirror.com`。
  本项目 models/ 已缓存，正常情况加载是纯本地的（实测 634~789ms）。
"""

import asyncio
import threading
import time
from pathlib import Path

from app.config import settings
from app.core.logging import logger

# 模型缓存目录：与 embedding 共用项目内 models/（见 embedding_service 的同名常量）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]      # app/services/x.py → 项目根
MODEL_CACHE_DIR = _PROJECT_ROOT / "models"

# 加载失败后的冷却时间（秒）。
#
# 为什么需要它：模型文件损坏 / 内存不足这类失败，重试基本治不好；
# 而"每个请求都去重试加载一次"会把一次 634ms 的失败成本乘以 QPS，
# 还会把日志刷爆（铁律 13④：降级高频时不许产生成堆堆栈）。
# 冷却期内直接快速失败 → 走降级路径；冷却期过后允许再试一次（半开，自愈）。
# ⚠ 反面：冷却期内即使问题已被修好（比如你把模型文件补上了），也要等满冷却才会恢复。
LOAD_RETRY_COOLDOWN_S = 60.0

# 进程级单例。三件东西必须一起看：
#   _model          —— 已加载的模型（None = 还没加载 / 加载失败）
#   _model_lock     —— 保护"判断 None + 构造"这个复合操作
#   _load_failed_at —— 上次加载失败的时刻（用于冷却判断）
_model = None
_model_lock = threading.Lock()
_load_failed_at: float | None = None


def _load_model():
    """
    返回重排模型单例（懒加载 + 双重检查锁 + 失败冷却）。

    为什么必须加锁（本项目 embedding 那个单例**没有**加锁，这里补上）：
    构造 `TextCrossEncoder` 就要加载 1GB 的 ONNX。两个请求同时首次到达时，
    "判断 _model is None"和"赋值 _model"之间隔着几百毫秒 ——
    没有锁的话两边都会看到 None，于是**各加载一份**，内存直接翻倍到 2GB。
    双重检查（锁外判一次、锁内再判一次）是为了让加载完成后的后续调用
    根本不进锁 —— 否则每次检索都要抢一次锁。

    为什么用 threading.Lock 而不是 asyncio.Lock：
    本函数是在 asyncio.to_thread 的**工作线程**里被调用的，
    那里没有事件循环，await 不了任何东西。
    """
    global _model, _load_failed_at

    if _model is not None:                  # 快路径：已加载，不进锁
        return _model

    with _model_lock:
        if _model is not None:              # 双重检查：等锁期间别人可能已经加载好了
            return _model

        if _load_failed_at is not None and (
            time.monotonic() - _load_failed_at
        ) < LOAD_RETRY_COOLDOWN_S:
            raise RuntimeError(
                f"重排模型处于加载失败冷却期（{LOAD_RETRY_COOLDOWN_S:.0f}s 内不重试）"
            )

        try:
            # 延迟 import：不检索就不加载 fastembed 的 rerank 子模块（D18 实测顶层未导出）
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(
                "首次加载重排模型: %s（cache_dir=%s）", settings.rerank_model, MODEL_CACHE_DIR
            )
            started = time.perf_counter()
            model = TextCrossEncoder(settings.rerank_model, cache_dir=str(MODEL_CACHE_DIR))
            elapsed_ms = (time.perf_counter() - started) * 1000
            _model = model
            _load_failed_at = None
            logger.info("重排模型加载完成 耗时=%.0fms", elapsed_ms)
            return model
        except Exception:
            # 记下失败时刻 → 后续 LOAD_RETRY_COOLDOWN_S 秒内快速失败，不再重试
            _load_failed_at = time.monotonic()
            raise


def _rerank_sync(query: str, documents: list[str]) -> list[float]:
    """
    同步打分（只负责算，不负责降级）—— 由 rerank() 丢进线程池执行。

    ⚠ `rerank()` 返回的是**生成器**：不消费它就一次前向都不会跑。
    D18 的延迟探针正是靠这一点避开了"测出接近 0 的数"（当时用 list() 强制消费）。
    这里必须 list()：我们要的是分数列表本身。
    """
    model = _load_model()
    return list(model.rerank(query, documents))


async def rerank(
    query: str,
    candidates: list[dict],
    top_n: int,
) -> tuple[list[dict], bool]:
    """
    对候选做精排，返回 (结果列表, 是否真的重排过)。

    端到端（入口 → 步骤 → 返回）：
        rerank("年假有几天", 融合后的20条, top_n=5)
          → 取出 20 条 content
          → asyncio.to_thread(_rerank_sync)        同步推理丢线程，不占事件循环
              → _load_model()                      懒加载单例（首次 634ms）
              → model.rerank(query, docs)          20 次 [query,doc] 前向 → 20 个 logit
          → 按 logit 降序（并列按 chunk_id）→ 取前 5
          → (5 条, True)

    降级（不影响调用方的代码路径，只是第二个返回值变 False）：
        超时 / 抛异常 / 分数条数与候选数不符 → (candidates[:top_n], False)

    Args:
        query: 用户问题（原始文本）
        candidates: 融合后的候选，**顺序即融合名次**（降级时直接沿用这个顺序）
        top_n: 重排后保留条数
    Returns:
        (结果列表, reranked)。reranked=True 时每条结果带 `rerank_score`（logit）。

    ⚠ 返回的是**新 dict**（浅拷贝），不原地改调用方传进来的融合结果：
      这样降级/成功两条路径对调用方的输入都没有副作用，调用方可以放心复用原列表。
    """
    if not candidates:
        # 空候选不加载模型：否则一次注定没结果的检索也要白付 634ms 的加载成本
        return [], False

    documents = [item.get("content") or "" for item in candidates]

    try:
        scores = await asyncio.wait_for(
            # 丢线程：ONNX 推理里一个 await 都没有，留在事件循环里就是把循环钉住 750ms
            asyncio.to_thread(_rerank_sync, query, documents),
            timeout=settings.rerank_timeout_s,
        )
    except asyncio.TimeoutError:
        # 注意：wait_for 只让**请求**提前返回，工作线程停不下来 ——
        # Python 线程无法被强制中断，它会把这次推理算完再丢弃结果。
        # 所以超时保护的是"请求延迟"，不是 CPU；持续超时会让线程池里越堆越多。
        logger.warning(
            "重排超时降级 timeout=%.1fs 候选=%d query=%r",
            settings.rerank_timeout_s, len(candidates), query[:20],
        )
        return [dict(item) for item in candidates[:top_n]], False
    except Exception as e:
        # 降级日志**不带堆栈**（铁律 13④）：这是高频可预期事件（模型没起来时每个请求都会走这里），
        # 带 exc_info 会把真正的故障埋进噪音里。只留异常消息。
        logger.warning("重排失败降级 err=%s 候选=%d query=%r", e, len(candidates), query[:20])
        return [dict(item) for item in candidates[:top_n]], False

    if len(scores) != len(candidates):
        # 防御性断言：分数与候选错位会让「内容 A 配上分数 B」静默发生，
        # 排序全乱但不报错。宁可降级。
        logger.warning(
            "重排分数条数不匹配 scores=%d candidates=%d，降级", len(scores), len(candidates)
        )
        return [dict(item) for item in candidates[:top_n]], False

    # 排序键：① 分数降序 ② chunk_id 升序兜底
    # 第②档的存在理由和 D17 融合一样 —— 只需保证"同一份数据两次跑出同一顺序"，
    # 不让并列的先后交给 Python 的稳定排序去碰运气。
    paired = sorted(
        zip(scores, candidates),
        key=lambda pair: (-pair[0], pair[1].get("chunk_id") or ""),
    )

    results: list[dict] = []
    for score, item in paired[:top_n]:
        enriched = dict(item)                        # 浅拷贝，见 docstring 的⚠
        enriched["rerank_score"] = round(float(score), 4)
        results.append(enriched)

    logger.info(
        "重排完成 候选=%d 返回=%d 分数区间=[%.4f, %.4f] query=%r",
        len(candidates), len(results),
        results[0]["rerank_score"] if results else 0.0,
        results[-1]["rerank_score"] if results else 0.0,
        query[:20],
    )
    return results, True
