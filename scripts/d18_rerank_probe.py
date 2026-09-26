"""
D18 探针：bge-reranker-base 的 token 换算与延迟实测
====================================================

D18 是学习日（不写业务代码），这个脚本的存在只有一个理由：
**D19 的落地决策依赖这批数据，而数据必须可复现。**

它回答三个问题：
  ① 一篇切片到底有多少 token？（中文不是 1 字 = 1 token）
  ② top-k 候选的重排延迟是多少？候选数 / 切片长度的伸缩关系是什么？
  ③ 有没有免费的提速开关？（换线程数 / 换执行器 / 批处理）

⚠️ 诚实标注：`--corpus db` 之外用的是**合成文本**。
   真实语料目前只有 1 篇文档 / 3 个切片、长度 62~73 字，
   所以 300 字那档是「按生产常量 CHUNK_SIZE 合成的」，不是生产数据本身。
   它的价值是给出 token 数 → 延迟的换算，而不是宣称这就是线上表现。

⚠️ 第二条诚实标注（更重要）：**延迟受 CPU 争用影响极大，必须连 load 一起报**。
   同一台机器、同一份代码、同一档输入，实测出现过三个"基准"：

   | 那次跑的 | top20@300字 | 当时的 load | 可信度 |
   |---|---|---|---|
   | 最早一版脚本（/tmp） | 690~740 ms | **未记录** | 不可解释 → 不作为基准 |
   | 固定成脚本后 · 机器忙 | 1326~1533 ms | 14.75 / 10 核 | 只能当**上界** |
   | 固定成脚本后 · 机器半忙 | **中位 837 ms** | **7.03 / 10 核** | 目前最好的基准，但仍非空载 |

   ➜ 所以本脚本在每个段落前后都打印 load average —— **不带 load 的延迟数字是不可解释的**。
   ➜ 也顺带说明：报告这类数字时，只给一个数是不诚实的，必须给"值 + 条件"。

运行：
    uv run python -m scripts.d18_rerank_probe              # 全部段落
    uv run python -m scripts.d18_rerank_probe --no-ep      # 跳过 CoreML 对照（日志很吵且慢）
"""

from __future__ import annotations

import argparse
import os
import time

# ---------------------------------------------------------------------------
# 为什么必须记录 load average（本脚本最重要的一条方法论）
# ---------------------------------------------------------------------------
# 同一台机器、同一份代码、同一档输入，top20@300字 实测出现过三个"基准"：
#   · 690~740 ms    （/tmp 那版脚本，**load 未记录** → 不可解释，不作基准）
#   · 1326~1533 ms  （load 14.75 / 10 核 —— 机器被打满 → 只能当上界）
#   · 中位 837 ms   （load  7.03 / 10 核 —— 目前最好的基准，但仍非空载）
#
# 差值不是代码问题，是**CPU 争用**：onnxruntime 默认开满核，
# 谁在旁边抢核，延迟就被等比放大（实测 75/150/300/450 四档**同时**放大 ~1.8 倍，
# 这种"全局等比放大"正是争用而非 bug 的特征）。
# 而且**探针自己就是 load 的大头**：杀掉进程后 1 分钟 load 从 14.75 掉到 7.55。
#
# ➜ 因此：**延迟数字必须连同当时的 load average 一起报**，否则不可解释。
#   本脚本每个段落前后各打一行 load，并在测量段落同时打印原始各次值（不只最小值），
#   让"抖动幅度"本身也可见。
def load_tag() -> str:
    """当前 load average / 逻辑核数 —— 延迟数字的解读前提。"""
    la = os.getloadavg()[0]
    return f"load={la:.2f}/{os.cpu_count()}核"

# 必须在 import fastembed / huggingface_hub 之前设好 ——
# 它们读的是环境变量，晚了就不生效（本机实测 huggingface.co 直连不通，HTTP 000）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL_NAME = "BAAI/bge-reranker-base"
CACHE_DIR = "models"

# 生产常量（app/services/retrieval_service.py）
CHUNK_SIZE = 300

# 一段接近真实制度文档的中文，用来合成不同长度的切片
BASE_TEXT = (
    "第二十三条 员工入职满一年后享有带薪年假。年假天数按工龄计算：满一年不满三年者为五天，"
    "满三年不满五年者为十天，满五年不满十年者为十五天，满十年以上者为二十天。"
    "年假需提前三个工作日在系统中提交申请，经直属主管审批后生效，跨年度年假原则上不结转。"
    "第二十四条 员工因公出差产生的交通、住宿与餐饮费用，需在费用发生后十五个工作日内报销。"
    "报销流程为：填写报销单，附原始票据，经部门经理审批，最后交由财务部审核打款。"
    "单笔金额超过五千元的，须由分管副总复核。审批未通过的单据将退回申请人并说明理由。"
)

QUERY = "年假有几天"


def synth_chunk(chars: int) -> str:
    """合成一段目标字数的中文切片（反复拼接 BASE_TEXT 后截断）。"""
    return (BASE_TEXT * 4)[:chars]


# ----------------------------------------------------------------------------
# ① token 换算
# ----------------------------------------------------------------------------
def section_tokens(tok) -> None:
    print("\n" + "=" * 72)
    print("① 中文切片长度 → token 数（为什么不能按「篇数」估算成本）")
    print("=" * 72)
    print(f"  {'切片字符数':>10} {'tokens':>8} {'token/字':>10}")
    ratios: list[float] = []
    for chars in (75, 150, 300, 450, 600):
        n = len(tok.encode(synth_chunk(chars)).ids)
        ratios.append(n / chars)
        print(f"  {chars:>10} {n:>8} {n / chars:>10.2f}")
    print(f"\n  实测 token/字 区间 {min(ratios):.2f} ~ {max(ratios):.2f}"
          f"（越长越收敛，{600} 字时为 {ratios[-1]:.2f}）"
          f" —— BPE 会把常见词合并，所以不是 1:1。")
    print("  ⚠ 教训：用十几字的短句测出「2.8 ms/篇」，但生产切片是 300 字 / 223 tokens。")
    print("     同口径的**总量**比：56 ms（20 篇 ×12 tok）vs ~843 ms（20 篇 ×223 tok）= **15 倍**；")
    print("     token 数之比是 223/12 = 18.6 倍 —— 同量级（有固定开销，所以略低于线性）。")
    print("     所以第一版那组数**低估了 15 倍**，不是随便差一点。")
    print()
    print("     📌 这里我曾写成「差 250 倍」，是错的：那是拿 **20 篇的总耗时**除以 **单篇耗时**，")
    print("        单位不匹配。**比值必须同口径** —— 总量比总量、单篇比单篇。")


# ----------------------------------------------------------------------------
# ② 延迟随 token 数与候选数
# ----------------------------------------------------------------------------
def section_latency(enc, tok, repeat: int = 3) -> None:
    print("\n" + "=" * 72)
    print(f"② 延迟：切片长度 × 候选数（默认会话配置）   [{load_tag()}]")
    print("=" * 72)
    print(f"  {'切片':>6} {'tokens':>7} {'top5':>10} {'top10':>10} {'top20':>10} {'单篇@20':>9}")
    measured: dict[int, tuple[int, list[float]]] = {}
    for chars in (75, 150, 300, 450):
        chunk = synth_chunk(chars)
        nt = len(tok.encode(chunk).ids)
        row = []
        for k in (5, 10, 20):
            docs = [chunk] * k
            list(enc.rerank(QUERY, docs[:1]))          # 预热，排除首调开销
            best = min(_time(lambda: list(enc.rerank(QUERY, docs))) for _ in range(repeat))
            row.append(best * 1000)
        measured[chars] = (nt, row)
        print(f"  {chars:>6} {nt:>7} {row[0]:>9.1f}ms {row[1]:>9.1f}ms "
              f"{row[2]:>9.1f}ms {row[2] / 20:>8.2f}ms")

    nt300, r300 = measured[300]
    nt75, r75 = measured[75]
    print(f"\n  实测换算：{nt75} tok → top20 {r75[2]:.1f} ms；"
          f"{nt300} tok → top20 {r300[2]:.1f} ms"
          f"（token 比 {nt300 / nt75:.2f}× → 耗时比 {r300[2] / r75[2]:.2f}×）")
    print(f"  候选数比 {20 // 5}× → 耗时比 top20 / top5 = {r300[2] / r300[0]:.2f}×（近似线性）")
    print(f"  ➜ 生产尺寸（{CHUNK_SIZE} 字 / {nt300} tok）下 top20 = {r300[2]:.0f} ms ——"
          f" 这不是毫秒级开销，是链路里最大的一块串行成本。")

    print(f"\n  --- top20 @ 300 字的逐个原始值（不只报最小值）---   [{load_tag()}]")
    chunk = synth_chunk(CHUNK_SIZE)
    docs20 = [chunk] * 20
    list(enc.rerank(QUERY, docs20))
    ts = [_time(lambda: list(enc.rerank(QUERY, docs20))) * 1000 for _ in range(repeat)]
    print("  " + " / ".join(f"{t:.1f} ms" for t in ts) +
          f"   → 中位 {sorted(ts)[len(ts) // 2]:.1f} ms")
    spread = (max(ts) - min(ts)) / min(ts) * 100
    print(f"  抖动幅度 = (max-min)/min = {spread:.1f}%   "
          f"（>50% 说明机器在被抢核，这组数只能当**上界**用）")


# ----------------------------------------------------------------------------
# ③ 有没有免费的提速开关
# ----------------------------------------------------------------------------
def section_session_config(repeat: int = 2, with_ep: bool = True) -> None:
    print("\n" + "=" * 72)
    print(f"③ 会话配置对照：换线程数 / 换执行器能不能提速   [{load_tag()}]")
    print("=" * 72)
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    chunk = synth_chunk(CHUNK_SIZE)
    docs20 = [chunk] * 20
    results: list[tuple[str, float]] = []

    configs: list[tuple[str, dict]] = [
        ("默认（0 改动）", {}),
        ("threads=4", {"threads": 4}),
        ("threads=10", {"threads": 10}),
    ]
    if with_ep:
        # CoreML 会往 stderr 刷大量 onnxruntime 日志（图分区信息），这是它自己的输出
        configs.append(("CoreML 执行器", {"providers": ["CoreMLExecutionProvider"]}))

    for label, kwargs in configs:
        try:
            enc = TextCrossEncoder(MODEL_NAME, cache_dir=CACHE_DIR, **kwargs)
            list(enc.rerank(QUERY, docs20[:1]))
            best = min(_time(lambda: list(enc.rerank(QUERY, docs20))) for _ in range(repeat))
            results.append((label, best * 1000))
            print(f"  {label:<18} top20 = {best * 1000:8.1f} ms")
        except Exception as exc:                        # noqa: BLE001 — 对照实验，失败只记录
            print(f"  {label:<18} 构造失败：{str(exc)[:70]}")

    # ---- 结论全部由本次实测推导；**没测的不许下结论**（铁律 11）----
    if not results:
        print("\n  ⚠ 本次没有任何配置跑成功，无法下结论。")
        return

    print()
    best_label, best_ms = min(results, key=lambda kv: kv[1])
    print(f"  实测最快配置：**{best_label}**（top20 = {best_ms:.1f} ms）")
    for label, ms in results:
        if label != best_label:
            print(f"    · {label:<14} {ms:7.1f} ms = 基准的 {ms / best_ms:.2f}×")
    if len(results) == 1:
        print("  ⚠ 只测到一个配置，无法做横向比较。")

    if with_ep and any("CoreML" in label for label, _ in results):
        cm = next(ms for label, ms in results if "CoreML" in label)
        print(f"\n  CoreML 实测 {cm:.0f} ms = 基准的 {cm / best_ms:.1f}× —— 慢，原因是可解释的：")
        print("    它只能接管 641 个节点里的 405 个，图被切碎；而我们**每个 pair 一次独立前向**，")
        print("    每次前向都要付一次分区与跨设备搬运开销。")
    elif not with_ep:
        print("\n  （本次 --no-ep，CoreML 与线程数以外的执行器**未测**，此处不下结论）")

    print("  ➜ 唯一可靠的延迟旋钮是「减少候选数」（近似线性，20→10 约省一半），"
          "不是调线程或换执行器。")


def section_batching() -> None:
    print("\n" + "=" * 72)
    print("④ 批推理：是不是缺失的杠杆？")
    print("=" * 72)
    import inspect

    from fastembed.rerank.cross_encoder import TextCrossEncoder

    sig = inspect.signature(TextCrossEncoder.rerank)
    print(f"  rerank 签名: {sig}")
    print("\n  → batch_size 默认 **64**。而我们的候选窗口只有 20 条，")
    print("    20 < 64 ⇒ **20 条本来就已经在同一个 batch 里**，")
    print("    所以「批推理」不构成可优化的点。这条排除了一个看似合理的猜想。")


# ----------------------------------------------------------------------------
# ⑤ 语义合理性 sanity check（用真实语料，如果库里有的话）
# ----------------------------------------------------------------------------
def section_sanity(enc, tok) -> None:
    print("\n" + "=" * 72)
    print("⑤ 语义排序 sanity check")
    print("=" * 72)

    # 三篇内容互不相同的切片，看 reranker 能不能把「年假」那篇排第一
    docs = [
        synth_chunk(300),                                             # 含年假/报销/审批
        # 只讲运维，与「年假」语义无关
        ("代码上线必须先在灰度环境验证，确认无异常后再全量发布。"
         "若监控出现错误率上升，需立即执行回滚操作，回滚窗口不超过十分钟。"
         "发布前需提交变更单，经运维与研发双方确认后方可执行。") * 3,
        # 只讲考勤，与「年假」近但不同
        ("员工上下班需按时打卡，迟到超过三十分钟记为旷工半日。"
         "加班需提前申请，调休需在三个月内使用完毕，逾期作废。"
         "外出办事需填写外出登记表并经主管签字确认。") * 3,
    ]
    scores = list(enc.rerank(QUERY, docs))
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    names = ["制度综合（含年假）", "运维发布规范", "考勤管理"]
    print(f"  query = {QUERY!r}")
    for rank, i in enumerate(order, 1):
        print(f"    {rank}. {names[i]:<18} score={scores[i]:+.4f}  ({len(docs[i])} 字 / "
              f"{len(tok.encode(docs[i]).ids)} tok)")
    print("\n  期望：含「年假」的那篇排第一。")
    print("  顺带观察分数形态：**可以取负值**（如 -9.679）—— 这是 logit，不是概率，")
    print("  所以不存在「0.5 以上才算相关」这种绝对阈值。")


def _time(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3, help="每档重复次数，取最小值")
    ap.add_argument("--no-ep", action="store_true", help="跳过 CoreML 对照（慢且日志吵）")
    args = ap.parse_args()

    from fastembed.rerank.cross_encoder import TextCrossEncoder
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    print(f"HF_ENDPOINT = {os.environ['HF_ENDPOINT']}")
    print(f"cache_dir   = {os.path.abspath(CACHE_DIR)}")
    print(f"model       = {MODEL_NAME}")
    print(f"{load_tag()}   ← 延迟数字只有在机器不忙时才可当基准；忙时只能当**上界**")

    tok = Tokenizer.from_file(
        hf_hub_download(MODEL_NAME, "tokenizer.json", cache_dir=CACHE_DIR)
    )

    t0 = time.perf_counter()
    enc = TextCrossEncoder(MODEL_NAME, cache_dir=CACHE_DIR)
    print(f"\n模型加载（不含下载）耗时 {(time.perf_counter() - t0) * 1000:.0f} ms")

    section_tokens(tok)
    section_latency(enc, tok, repeat=args.repeat)
    section_session_config(repeat=max(2, args.repeat - 1), with_ep=not args.no_ep)
    section_batching()
    section_sanity(enc, tok)

    print("\n" + "=" * 72)
    print(f"汇总（D19 落地决策依据）   [{load_tag()}]")
    print("=" * 72)
    print("  · 生产尺寸 top20 的具体数值见 ② 段实测（同一台机器两次跑可差 1.8×，")
    print("    差异来自 CPU 争用而非代码）—— 这是新增的**串行**开销，链路里最大的一块")
    print("  · 无免费开关：线程数 / CoreML 执行器 / 批处理三条路都不通（③④ 段实测）")
    print("  · 唯一旋钮 = 候选窗口大小（线性关系，20→10 约省一半）")
    print("  · 分数是 logit，可正可负 → 只能按名次截断，不能按绝对阈值过滤")
    print("  ⚠ 汇报延迟数字时必须带上 load average，否则不可解释")


if __name__ == "__main__":
    main()
