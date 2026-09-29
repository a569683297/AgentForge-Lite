"""
judge 评分器（D22）—— 把"判得准不准"这件事变成一条可重复的流水线
====================================================================
输入：题目 + 参考答案 + 检索片段 + 待评答案
输出：三个维度的分数（correctness / faithfulness / completeness）+ 每次的原始分数

--------------------------------------------------------------------------
这个模块与 D20 探针脚本的关系
--------------------------------------------------------------------------
D20 的 `scripts/d20_judge_probe.py` 是**量具校准**：它回答了"裁判抖多少"，
所以它调用的样本是**人造的**（明显对/明显错/边界/难度梯度），目的是把裁判逼到极限。
D22 这个模块是**真正开始量**：样本是被测系统真实产出的答案。

两者的 rubric **必须是同一份文本** —— rubric 是量具的定义，两处各写一份
就会出现"校准用的是 A 版量具、真正测量用的是 B 版"这种致命错位，
而且没有任何报错。所以这里把 rubric 作为**唯一权威**保留，
D20 脚本里的那份是它的历史副本（探针结果只对当时那版 rubric 有效）。

--------------------------------------------------------------------------
为什么 judge 必须走 Gateway 且锁定通道
--------------------------------------------------------------------------
① 走 Gateway —— PRD §8.2 的架构红线（不得绕过）。绕过就没有 Langfuse 追踪、
   token 统计与统一超时。D20 探针当时绕过了（它是量具校准、要显式控制两个通道），
   D22 起改为给 Gateway 加锁定能力，不再绕过。
② 锁定通道 —— 见 `app/config.py::llm_channel` 的说明：judge 一旦走自动降级，
   "一部分样本 A 打的分、一部分 B 打的分"就会发生，而报告上只写一个名字。

--------------------------------------------------------------------------
聚合口径：中位数 ≥ 阈值 ≡ 多数投票
--------------------------------------------------------------------------
D20 定的规则是"3 次多数投票"（每次二值化为 通过/不通过，再取多数）。
本模块的做法是"取三维度分数的中位数，再按 PASS_THRESHOLD 二值化"。

两者在数学上等价（阈值是单点、比较是单调的）：
    中位数 ≥ 4  ⟺  至少半数以上的次数 ≥ 4  ⟺  多数投票通过
取中位数的额外好处是**同时得到分数值**（报告要报均分），而多数投票只给 0/1。
"""

import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings
from app.core.logging import logger
from app.services.llm_gateway import chat

# ============================================================
# rubric（量具的定义 —— 唯一权威，改动它 = 量具换代，历史分数不可比）
# ============================================================
# 这份文本与 scripts/d20_judge_probe.py 中的是同源副本。第 2 轮探针修订过两处：
#   ① 1 分 / 4 分档的判据写具体（原先"轻微不准确"太模糊，导致分数聚集在 4 分附近）
#   ② 负例单独给了明确规则（正确拒答 = correctness 5，编造 = 0~1）
# 没有这两条时，探针第 1 轮出现"阈值带为空 → ε 恒等于 0"的假数。
JUDGE_SYSTEM = """你是一个严格的评测裁判。你会收到：题目、参考答案、检索到的片段、待评答案。

请对以下三个维度各打 0-5 分（整数）。**每一档都必须按下面写明的判据来打，不要自行发挥。**

correctness（正确性）—— 答案与【参考答案】比，**只看关键事实对不对**：
  5 = 关键事实与参考答案完全一致，无任何出入
  4 = 关键事实正确，但存在下列之一：(a) 表述比参考答案更简略或更啰嗦，实质信息相同；
      (b) 附带了一句参考答案中没有、但与参考答案不冲突的常识性补充
  3 = 关键结论正确，但遗漏或含混了参考答案中的限定条件（适用人群 / 时限 / 例外条款）
  2 = 关键事实有错误（数值、条款、结论至少一项错），但方向沾边、能看出在回答该问题
  1 = 与参考答案明确冲突 —— 给出了与参考答案相反或不同的关键数值/结论
      例：参考答案写「5 天」，答案写「20 天」或「全额报销，没有上限」
  0 = **完全没有回答该问题** —— 跑题、纯空话（如「建议咨询 HR」）、或未给出任何实质内容

faithfulness（引用忠实度）—— 答案与【检索到的片段】比：
  5 = 所有事实性陈述都能在片段中找到依据
  4 = 事实性陈述均有依据，仅措辞、连接语或泛指表述超出片段，未引入新的事实
  3 = 大部分有依据，但有一处片段里没有的具体细节（数字 / 条款 / 时限）
  2 = 有编造的实质性内容（编造的数字、条款、结论）
  1 = 多处实质性编造（两项以上事实性内容均无片段依据）
  0 = 整段答案找不到任何片段依据

completeness（完整性）—— 答案与【参考答案的要点】比：
  5 = 要点全覆盖
  4 = 覆盖了主要要点，仅缺少非关键的次要细节
  3 = 漏掉一个要点
  2 = 漏掉多个要点
  1 = 只提到题目相关的个别词句，未形成有效回答
  0 = 未回答 / 无实质内容

硬性要求：
- **答案的长度、语气、格式一律不计分**，只看内容。写得多不等于分高，写得少不等于分低。
- 打分必须先确定"关键事实是什么"，再逐档对照；不要在档位之间折中取整。
- 负例（参考答案写着"文档中无答案"）时：正确拒答 = correctness 5；编造内容 = correctness 0~1。

只输出一个 JSON 对象，不要任何其他文字、不要代码块围栏：
{"correctness": <int>, "faithfulness": <int>, "completeness": <int>, "reason": "<一句话>"}"""

DIMENSIONS: tuple[str, ...] = ("correctness", "faithfulness", "completeness")

# ============================================================
# 重试
# ============================================================
# 判据：**再试一次可能成功**，不是"重试会更快"。
#   429 限流 → 等一会儿就有额度；5xx → 服务端临时故障；超时/网络 → 连接偶发失败
# 反例（不重试）：401 鉴权失败、400 参数错、模型名不存在 —— 重试 100 次都是同样的错，
# 只会把延迟放大 3 倍并把真正的配置问题埋进日志。
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
RETRY_TIMES = 2            # 最多重试 2 次（共 3 次尝试）
RETRY_BASE_S = 1.0         # 指数退避基数：1s → 3s

_JSON_RE = re.compile(r"\{.*\}", re.S)


# ============================================================
# 单次打分
# ============================================================
def build_prompt(*, question: str, reference: str, chunks: str, answer: str) -> str:
    """把四样东西拼成 judge 的 user 消息。顺序与 D20 探针一致（校准结论才对得上）。"""
    return (
        f"【题目】\n{question}\n\n"
        f"【参考答案】\n{reference}\n\n"
        f"【检索到的片段】\n{chunks}\n\n"
        f"【待评答案】\n{answer}\n\n"
        "请按要求输出 JSON。"
    )


def parse_scores(raw: str) -> dict:
    """
    从模型输出里抠出 JSON。**失败不抛异常** —— 打不通/格式坏本身就是要观测的现象。

    为什么"格式坏"要被记录而不是被掩盖：如果一个 judge 有 3% 的输出不是合法 JSON，
    把它静默丢掉会让"成功率"永远显示 100%，而分母悄悄变小 ——
    于是三个配置的分母不同，分数就不可比了。这与 D19 那条
    "降级样本被算成正常成绩"是同一族问题。
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    match = _JSON_RE.search(text)
    if not match:
        return {"ok": False, "error": f"无 JSON：{text[:80]!r}"}
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"JSON 解析失败：{exc}"}

    out: dict[str, Any] = {}
    for dim in DIMENSIONS:
        value = obj.get(dim)
        # isinstance(v, bool) 也要排除：Python 里 True 是 int 的子类，
        # 不排掉的话 judge 输出 {"correctness": true} 会被当成 1 分收下。
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {"ok": False, "error": f"维度 {dim} 不是数字：{value!r}"}
        out[dim] = int(value)
    out["ok"] = True
    out["reason"] = str(obj.get("reason", ""))[:60]
    return out


def _is_retryable(err: Exception) -> bool:
    if isinstance(err, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code in _RETRYABLE_STATUS
    return False


async def judge_once(
    *,
    question: str,
    reference: str,
    chunks: str,
    answer: str,
    judge_model: str | None = None,
) -> dict:
    """
    打一次分。

    Returns:
        {"ok": True, "correctness": n, "faithfulness": n, "completeness": n, "reason": s,
         "elapsed": float}
        或 {"ok": False, "error": str, "elapsed": float}

    ⚠ temperature=0（PRD §8.4 的 judge 参数）。注意它**不等于确定性** ——
      D20 实测同一份输入 5 次仍会翻转，这正是 ε 存在的原因。
    """
    model_name = judge_model or settings.judge_model
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": build_prompt(
            question=question, reference=reference, chunks=chunks, answer=answer
        )},
    ]

    started = time.perf_counter()
    last_error: Exception | None = None

    for attempt in range(RETRY_TIMES + 1):
        try:
            content = await chat(
                messages,
                trace_name="eval-judge",
                temperature=0.0,
                provider=model_name,      # ← 锁定通道：失败就抛，绝不偷偷换模型
            )
        except Exception as exc:  # noqa: BLE001 —— 失败也是一条观测
            last_error = exc
            if not _is_retryable(exc) or attempt == RETRY_TIMES:
                break
            await asyncio.sleep(RETRY_BASE_S * (3 ** attempt))
            continue

        parsed = parse_scores(content)
        parsed["elapsed"] = time.perf_counter() - started
        if parsed.get("ok"):
            return parsed

        # 格式坏：也重试一次 —— 大模型偶发吐非 JSON，重试往往就好了。
        # 但它**不算网络故障**，所以单独判、并且不掩盖（错误文本会带进 raw）。
        last_error = RuntimeError(parsed.get("error", "未知解析错误"))
        if attempt == RETRY_TIMES:
            parsed["elapsed"] = time.perf_counter() - started
            return parsed

    assert last_error is not None
    logger.warning("judge 调用失败 model=%s err=%s", model_name, last_error)
    return {
        "ok": False,
        "error": f"{type(last_error).__name__}: {last_error}",
        "elapsed": time.perf_counter() - started,
    }


# ============================================================
# 聚合
# ============================================================
@dataclass
class JudgeOutcome:
    """一条答案的多次打分聚合结果。"""

    correctness: float | None = None
    faithfulness: float | None = None
    completeness: float | None = None
    runs_ok: int = 0
    runs_total: int = 0
    raw: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_elapsed: float = 0.0

    @property
    def all_failed(self) -> bool:
        return self.runs_ok == 0

    def median_of(self, dim: str) -> float | None:
        """某一维度各次分数的中位数（只用成功的那些次）。"""
        values = [r[dim] for r in self.raw if r.get("ok")]
        return statistics.median(values) if values else None


def aggregate(results: list[dict]) -> JudgeOutcome:
    """
    把同一份答案的多次打分聚合成一个结论。

    为什么取**中位数**而不是平均分：
        平均分对极端值敏感。3 次里出现一次离谱的 0 分（judge 抽风），
        平均分会被拉到 2.67，而中位数仍是 4 —— 判定结论完全不同。
        中位数回答的是"多数次怎么判"，这正是"多数投票"的连续版本。
    """
    outcome = JudgeOutcome(runs_total=len(results))
    for result in results:
        outcome.raw.append(result)
        outcome.total_elapsed += float(result.get("elapsed") or 0.0)
        if result.get("ok"):
            outcome.runs_ok += 1
        else:
            outcome.errors.append(str(result.get("error", "未知错误")))

    if outcome.runs_ok:
        outcome.correctness = outcome.median_of("correctness")
        outcome.faithfulness = outcome.median_of("faithfulness")
        outcome.completeness = outcome.median_of("completeness")
    return outcome
