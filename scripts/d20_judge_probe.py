"""
D20 探针：judge 这把尺子的误差有多大
======================================

**它只回答一个问题**：在 50 条题的消融对比里，差几条才算"真差异"？

做法（三步）：
  ① SAMPLES 里的每份样本答案 × RUNS 次重复 × 2 个 judge —— 全部 temperature=0
  ② 用「多次多数投票」当作这道题的**真值** → 反推 judge **单次判定的错误率 ε**
  ③ 由 ε 推出**噪声地板**：两个配置各 EVAL_SET_SIZE 条时，纯噪声期望制造出多少条差异

--------------------------------------------------------------------------
第 1 轮（2026-09-27）踩的坑 —— 重跑前必读
--------------------------------------------------------------------------
第 1 轮跑出 ε = 0.0000、噪声地板 0.00 条，**这个数是假的**：

    judge 实际动用分档 = {0, 2, 3, 5}  ← 没有 1、**没有 4**
    二值化阈值 = correctness ≥ 4      ← 阈值带（3.5~4.5）里一条样本都没有

阈值带空 ⇒ 单次判定跨阈概率**必然为 0**，与 judge 稳不稳毫无关系。
根因是**标签是我贴的、分是 judge 打的** —— 我标成"边界"的样本，在 judge 眼里根本不边界。

⇒ 所以样本组里必须有一组「**难度梯度**」（G1~G7）：同一问题、同一片段上，
   答案质量从满分逐档降到跑题，把"关键事实对但漏限定条件 / 带无冲突补充"这一档
   真正打进 4 分附近。**判断本轮数据可用不可用，先看那张分档直方图。**
   （与 D18「拿 12 token 的句子测出 2.8ms」同族：测量对象选错 → 看似精确实则荒谬的数。）

样本构成：
  4 份明显对 / 4 份明显错（sanity：这两组若抖动大，说明 rubric 或模型有问题）
  4 份边界（对但不全 / 轻微编造 / 长答案 / 极简答案）
  2 份负例（正确拒答 vs 硬编造）
  7 份难度梯度 G1~G7（第 2 轮新增，压阈值带用）

--------------------------------------------------------------------------
重试策略（第 2 轮新增）
--------------------------------------------------------------------------
relay 通道第 1 轮有 20% 调用失败（429 + 超时），两个样本 5 次全灭。丢弃 = 分母变化
= 三个配置不可比。所以要重试，但**报告必须同时给出"若不重试会失败多少次"**，
否则看起来只是"relay 挺稳"，把"它本来会丢两成样本"这件事掩盖掉。

--------------------------------------------------------------------------
耗时口径（第 2 轮新增，踩过坑）
--------------------------------------------------------------------------
两个口径必须分开报，**不能只报一个**：

    request = 真正的一次 HTTP 往返（拿到并发位之后才开始计时）
    e2e     = 从入队到返回（含排队等待 + 重试退避）

第 2 轮第一次跑时，`request` 的计时起点被写在了 `async with sem` **外面**，
于是一百多个任务同时入队抢 4 个并发位时，**排队时间被算进了"单次耗时"**，
印出「中位 11.74s、墙钟 23.5s」这种自相矛盾的组合
（105 × 11.74 ÷ 4 ≈ 308s，与 23.5s 差了 13 倍）。
脚本现在内置**口径自检**：比较 `总调用 × 中位 ÷ 并发` 与实测墙钟，
比值 ≈ 1 才算口径自洽。**这类错不报错、不崩，只是数字悄悄错掉。**

--------------------------------------------------------------------------
为什么顺带能测"偏差"
--------------------------------------------------------------------------
样本成对设计，本身构成对照：
  [V] 答得长 vs [T] 答得短，内容等价  → 看 judge 是否偏爱长答案（verbosity bias）
  [R] 负例正确拒答 vs [H] 负例硬编答案 → 看 judge 是否把"拒答"判对、"编造"判错

--------------------------------------------------------------------------
两条硬纪律（不遵守结论就作废）
--------------------------------------------------------------------------
1. **显式锁通道，绝不走 _call_with_failover**。
   网关的降级语义是"主通道失败 → 静默切备用"。judge 若走它，
   会出现"一部分样本是 DeepSeek 打的、一部分是中转 GPT 打的"，而报告上只写一个 judge 名字。
   → 探针自己发请求，只用 provider 字典指定的那一个。
2. **结论一律从实测数据现算**，不许在打印语句里写死区间（D18 已犯过两次）。

用法：
    uv run python -m scripts.d20_judge_probe
    uv run python -m scripts.d20_judge_probe --runs 5 --judges deepseek,relay-gpt
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time

import httpx

from app.config import settings

# ============================================================
# 配置
# ============================================================
RUNS = 5                     # 每个样本重复打几次（3 次只能看"有没有翻转"，5 次才能估比例）
CONCURRENCY = 4              # 并发上限：中转站不宜压满，也避免本机 load 失控
JUDGE_TEMPERATURE = 0.0      # PRD §8.4：评测 judge temp=0
REQUEST_TIMEOUT = 90.0

# 重试（第 2 轮新增）：第 1 轮 relay 通道有 20% 调用失败（429 + 超时），
# 其中两个样本 5 次全灭 —— 分母一变三个配置就不可比了（D19 Q3 同族问题）。
# 所以失败不能就这么丢掉，必须重试；但**必须同时报"重试把成功率抬到多少"**，
# 否则看起来只是"relay 挺稳"，掩盖了它本来会丢两成样本这个事实。
RETRY_TIMES = 2              # 最多重试 2 次（共 3 次尝试）
RETRY_BASE_S = 1.0           # 指数退避基数：1s → 3s
RETRY_AFTER_CAP_S = 15.0     # 服务端 Retry-After 的采信上限（更长的等待不如直接判失败）

# 二值化阈值（PRD F7.5）：correctness ≥ 4 记"对"
PASS_THRESHOLD = 4

# 消融规模（PRD F7.1）：50 条题
EVAL_SET_SIZE = 50

DIMENSIONS = ("correctness", "faithfulness", "completeness")

# ============================================================
# judge 的 rubric（三份样本组共用同一份，改动它=改动量具，须整体重跑）
# ============================================================
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


# ============================================================
# 样本：12 份，4 明显对 / 4 明显错 / 4 边界
# ============================================================
# 统一背景：某公司《员工考勤与休假管理办法》，年假条款为「入职满 1 年 5 天」
_CHUNK = (
    "第 12 条 年休假：员工入职满 1 年不满 10 年的，每年享受带薪年休假 5 天；"
    "满 10 年不满 20 年的，10 天；满 20 年的，15 天。"
    "第 13 条 年休假原则上应在当年内休完，因工作需要未休完的，经部门负责人批准可顺延至次年 3 月 31 日前。"
    "第 14 条 员工累计工作满 1 年不满 10 年，请病假累计 2 个月以上的，不享受当年年休假。"
)
_QUESTION = "入职满 1 年的员工，每年有多少天带薪年休假？"
_REFERENCE = "入职满 1 年不满 10 年的，每年 5 天带薪年休假。"

_NEG_CHUNK = (
    "第 20 条 差旅费：市内交通凭票报销，单次不超过 200 元。"
    "第 21 条 住宿标准：一线城市每晚不超过 600 元，其他城市不超过 400 元。"
)
_NEG_QUESTION = "公司对员工子女的教育补贴是怎么规定的？"
_NEG_REFERENCE = "文档中没有关于员工子女教育补贴的任何规定。"

SAMPLES: list[dict] = [
    # ---- 组 1：明显对（sanity：这组若翻转，问题在量具不在被测对象）----
    {
        "id": "E1", "group": "easy_ok", "expect": "correctness 应稳定 ≥4",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年不满 10 年的员工，每年可享受带薪年休假 5 天。",
    },
    {
        "id": "E2", "group": "easy_ok", "expect": "correctness 应稳定 ≥4",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "根据第 12 条，入职满 1 年不满 10 年的员工每年有 5 天带薪年休假。",
    },
    {
        "id": "E3", "group": "easy_ok", "expect": "correctness 应稳定 ≥4",
        "question": "年休假一般要在什么时候休完？",
        "reference": "原则上当年内休完；确因工作需要未休完的，经部门负责人批准可顺延至次年 3 月 31 日前。",
        "chunks": _CHUNK,
        "answer": "原则上应当在当年内休完。如果因为工作原因实在没法休完，经部门负责人批准，可以顺延到次年 3 月 31 日之前。",
    },
    {
        "id": "E4", "group": "easy_ok", "expect": "correctness 应稳定 ≥4",
        "question": "满 10 年不满 20 年的员工年休假多少天？",
        "reference": "10 天。", "chunks": _CHUNK,
        "answer": "10 天。",
    },

    # ---- 组 2：明显错 ----
    {
        "id": "B1", "group": "easy_bad", "expect": "correctness 应稳定 ≤2",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年的员工每年有 20 天带薪年休假，工作满 3 年后增加到 25 天。",
    },
    {
        "id": "B2", "group": "easy_bad", "expect": "correctness 应稳定 ≤2",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "这取决于绩效考核结果，绩效 A 的员工可以额外申请 3 天，具体请咨询直属主管。",
    },
    {
        "id": "B3", "group": "easy_bad", "expect": "correctness 应稳定 ≤2",
        "question": "住宿报销标准是多少？",
        "reference": "一线城市每晚不超过 600 元，其他城市不超过 400 元。",
        "chunks": _NEG_CHUNK,
        "answer": "住宿费全额实报实销，没有上限，但需要提前在系统里提交预算审批单。",
    },
    {
        "id": "B4", "group": "easy_bad", "expect": "correctness 应稳定 ≤2",
        "question": "病假累计多久就不享受当年年休假？",
        "reference": "累计工作满 1 年不满 10 年、请病假累计 2 个月以上的，不享受当年年休假。",
        "chunks": _CHUNK,
        "answer": "这个制度里没有相关规定。",
    },

    # ---- 组 3：边界（真正会翻转的一档）----
    {
        "id": "C1", "group": "boundary", "expect": "对但不全：completeness 应偏低",
        "question": "年休假什么时候休完？如果没休完怎么办？",
        "reference": "原则上当年内休完；因工作需要未休完的，经部门负责人批准可顺延至次年 3 月 31 日前。",
        "chunks": _CHUNK,
        "answer": "应当在当年内休完。",
    },
    {
        "id": "C2", "group": "boundary", "expect": "对但轻微超标：faithfulness 应偏低",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年不满 10 年的员工每年有 5 天年休假，折算下来大约是每月 0.42 天，未休部分按日工资 300% 支付。",
    },
    {
        "id": "C3", "group": "boundary", "expect": "对但含无关内容：correctness 仍应 ≥4",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": (
            "入职满 1 年不满 10 年的员工每年有 5 天带薪年休假。\n"
            "补充说明：年休假的安排应当兼顾工作需要与个人意愿，部门应提前统筹排班，避免集中休假影响业务连续性。"
            "员工在提出休假申请时，建议至少提前 3 个工作日提交，以便主管安排工作交接。"
            "对于跨年度的休假安排，应当以书面形式确认，并留存归档。\n"
            "总之，年休假制度是公司关怀员工的重要体现，请大家合理安排。"
        ),
    },
    {
        "id": "C4", "group": "boundary", "expect": "对但极简：correctness 仍应 ≥4（长度不该扣分）",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "5 天。",
    },

    # ---- 组 4：负例（正确答案是拒答）----
    {
        "id": "N1", "group": "negative", "expect": "正确拒答：correctness 应稳定 ≥4",
        "question": _NEG_QUESTION, "reference": _NEG_REFERENCE, "chunks": _NEG_CHUNK,
        "answer": "检索到的文档片段中没有关于员工子女教育补贴的规定，我无法回答这个问题。",
    },
    {
        "id": "N2", "group": "negative", "expect": "硬编造：correctness 应稳定 ≤1",
        "question": _NEG_QUESTION, "reference": _NEG_REFERENCE, "chunks": _NEG_CHUNK,
        "answer": "公司为员工子女提供教育补贴：幼儿园阶段每月 500 元，义务教育阶段每月 800 元，高中阶段每年一次性补贴 3000 元。",
    },

    # ---- 组 5：难度梯度（第 2 轮新增）----
    # 为什么必须加这一组：第 1 轮 ε=0 是**假数** —— judge 实际用到的分数档是 {0,2,3,5}，
    # **4 分档一条都没有**，而二值化阈值恰好是 ≥4。阈值带（3.5~4.5）空着，
    # 单次判定当然不会翻转，这跟 judge 稳不稳毫无关系。
    #   修法不是把上面的样本重新贴标签（标签是我贴的，分是 judge 打的），
    #   而是**造一批答案质量连续变化的样本**，让"关键事实正确但有限定条件缺失 / 有附带补充"
    #   这一类真正落进 4 分附近。G1→G7 是同一个问题、同一条片段上的质量递减谱。
    {
        "id": "G1", "group": "gradient", "expect": "满分锚点：correctness 应 =5",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年不满 10 年的员工，每年可享受带薪年休假 5 天。",
    },
    {
        "id": "G2", "group": "gradient", "expect": "正确 + 无冲突补充：correctness 应 4~5",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年不满 10 年的员工，每年可享受带薪年休假 5 天，具体按公司制度执行。",
    },
    {
        "id": "G3", "group": "gradient", "expect": "★阈值带：漏掉限定条件，correctness 应 3~4",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "员工每年可以休 5 天带薪年假。",
    },
    {
        "id": "G4", "group": "gradient", "expect": "★阈值带：数字对 + 一句无据但不冲突的补充，correctness 应 4",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年不满 10 年的员工每年有 5 天年休假，休假期间工资照常发放。",
    },
    {
        "id": "G5", "group": "gradient", "expect": "★阈值带：数字对但附带与片段冲突的结论，correctness 应 2~3",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年不满 10 年的员工每年有 5 天年休假，未休完的部分可以在次年折算成工资发放。",
    },
    {
        "id": "G6", "group": "gradient", "expect": "明确冲突：correctness 应 =1（不是 0，它在正面回答问题）",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "入职满 1 年的员工每年有 15 天带薪年休假，比行业平均水平高一档。",
    },
    {
        "id": "G7", "group": "gradient", "expect": "空话锚点：correctness 应 =0",
        "question": _QUESTION, "reference": _REFERENCE, "chunks": _CHUNK,
        "answer": "这个问题建议你咨询人力资源部，每个人的情况不太一样。",
    },
]


# ============================================================
# judge 通道（显式指定，不经过网关的自动降级）
# ============================================================
def build_judges(names: list[str]) -> list[dict]:
    """按名字构造 judge 通道。deepseek=主通道；relay-gpt=中转的 OpenAI 兼容通道。"""
    all_judges = {
        "deepseek": {
            "name": "deepseek",
            "api_key": settings.deepseek_api_key,
            "base_url": settings.deepseek_base_url,
            "model": settings.deepseek_chat_model,
        },
        "relay-gpt": {
            "name": "relay-gpt",
            "api_key": settings.openai_api_key,
            "base_url": settings.openai_base_url,
            "model": settings.openai_chat_model,
        },
    }
    picked = []
    for n in names:
        if n not in all_judges:
            raise SystemExit(f"未知 judge：{n}（可选：{', '.join(all_judges)}）")
        j = all_judges[n]
        if not j["api_key"]:
            print(f"[skip] judge {n} 未配置 api_key，跳过")
            continue
        picked.append(j)
    if not picked:
        raise SystemExit("没有任何可用的 judge 通道")
    return picked


def build_prompt(sample: dict) -> str:
    return (
        f"【题目】\n{sample['question']}\n\n"
        f"【参考答案】\n{sample['reference']}\n\n"
        f"【检索到的片段】\n{sample['chunks']}\n\n"
        f"【待评答案】\n{sample['answer']}\n\n"
        "请按要求输出 JSON。"
    )


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_scores(raw: str) -> dict:
    """从中转内容里抠出 JSON。失败不抛异常 —— 打不通/格式坏本身就是要测的现象。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = _JSON_RE.search(text)
    if not m:
        return {"ok": False, "error": f"无 JSON：{text[:80]!r}"}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON 解析失败：{e}"}
    out = {}
    for d in DIMENSIONS:
        v = obj.get(d)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return {"ok": False, "error": f"维度 {d} 不是数字：{v!r}"}
        out[d] = int(v)
    out["ok"] = True
    out["reason"] = str(obj.get("reason", ""))[:60]
    return out


# 可重试的错误 —— 判据是"再试一次**可能**成功"，不是"重试会更快"
#   429 限流：等一会儿就有额度
#   5xx     ：服务端临时故障
#   超时/网络：连接偶发失败
# 反例（**不重试**）：401 鉴权失败、400 参数错、模型名不存在 —— 重试 100 次都是同样的错，
# 只会把延迟放大 3 倍并把真正的配置问题埋进日志。
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _is_retryable(err: Exception) -> bool:
    if isinstance(err, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code in _RETRYABLE_STATUS
    return False


def _backoff_delay(attempt: int, err: Exception) -> float:
    """退避时长。优先听服务端的 Retry-After，否则指数退避 1s → 3s。"""
    if isinstance(err, httpx.HTTPStatusError):
        raw = err.response.headers.get("retry-after")
        if raw:
            try:
                return min(float(raw), RETRY_AFTER_CAP_S)
            except ValueError:
                pass                                    # 有些站返回的是 HTTP-date，忽略
    return RETRY_BASE_S * (3 ** attempt)


async def judge_once(client, provider, sample, sem, stats) -> dict:
    # 两个计时口径，必须分开 —— 混在一起的后果实测过：
    #   request  = 真正的一次 HTTP 往返（拿到并发位之后才开始计）
    #   e2e      = 从入队到返回，**含排队 + 重试退避**
    # 若把 request 的起点写在 sem 外面，105 个任务同时入队抢 4 个并发位，
    # 排在后面的任务会把"排队时间"记进"单次耗时" —— 实测出现
    # 「中位 11.74s、墙钟 23.5s」这种自相矛盾的组合（11.74 × 105 ÷ 4 ≈ 308s ≫ 23.5s）。
    enqueued = time.perf_counter()
    last_error: Exception | None = None

    for attempt in range(RETRY_TIMES + 1):
        try:
            # sem 只在**单次请求**期间持有：退避等待时让出，否则睡 3 秒还占着并发位
            async with sem:
                started = time.perf_counter()       # ← 拿到并发位才开始计时
                resp = await client.post(
                    f"{provider['base_url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {provider['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "temperature": JUDGE_TEMPERATURE,
                        "messages": [
                            {"role": "system", "content": JUDGE_SYSTEM},
                            {"role": "user", "content": build_prompt(sample)},
                        ],
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
        except Exception as e:                          # noqa: BLE001 —— 失败也是一条观测
            last_error = e
            if not _is_retryable(e) or attempt == RETRY_TIMES:
                break
            stats["retries"] += 1
            await asyncio.sleep(_backoff_delay(attempt, e))
            continue

        if attempt > 0:
            stats["retry_recovered"] += 1
        stats["request"].append(time.perf_counter() - started)
        stats["e2e"].append(time.perf_counter() - enqueued)
        return parse_scores(content)

    assert last_error is not None
    stats["errors"].append(
        f"{provider['name']}/{sample['id']}: {type(last_error).__name__}: {last_error}"
    )
    return {
        "ok": False,
        "error": str(last_error),
        "elapsed": time.perf_counter() - enqueued,
        "tag": sample["id"],
    }


# ============================================================
# 统计
# ============================================================
def majority(binary: list[int]) -> int:
    """5 次二值结论的多数投票 → 当作该题真值。"""
    return 1 if sum(binary) * 2 > len(binary) else 0


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 区间 —— 小样本下比正态近似稳。"""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ============================================================
# 主流程
# ============================================================
async def run(runs: int, judge_names: list[str]) -> None:
    judges = build_judges(judge_names)

    print("=" * 78)
    print("D20 探针：judge 的测量误差有多大")
    print("=" * 78)
    # 组别构成现算，不写死 —— 改 SAMPLES 时这里自动跟着变
    group_counts: dict[str, int] = {}
    for s in SAMPLES:
        group_counts[s["group"]] = group_counts.get(s["group"], 0) + 1
    composition = " / ".join(f"{k} {v}" for k, v in group_counts.items())
    print(f"样本 {len(SAMPLES)} 份（{composition}）")
    print(f"重复 {runs} 次 × judge {len(judges)} 个 × temperature={JUDGE_TEMPERATURE}")
    print(f"预计调用数 = {len(SAMPLES) * runs * len(judges)}")
    for j in judges:
        print(f"  judge={j['name']:10s} model={j['model']:16s} base_url={j['base_url']}")
    print()

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict[str, list[dict]]] = {}

    async with httpx.AsyncClient() as client:
        for provider in judges:
            stats = {"request": [], "e2e": [], "errors": [], "retries": 0, "retry_recovered": 0}
            tasks = []
            for sample in SAMPLES:
                for _ in range(runs):
                    tasks.append(judge_once(client, provider, sample, sem, stats))
            started = time.perf_counter()
            settled = await asyncio.gather(*tasks)
            wall = time.perf_counter() - started

            # 摊平成 {sample_id: [每次的结果, ...]}
            by_sample: dict[str, list[dict]] = {s["id"]: [] for s in SAMPLES}
            idx = 0
            for sample in SAMPLES:
                for _ in range(runs):
                    by_sample[sample["id"]].append(settled[idx])
                    idx += 1

            results[provider["name"]] = by_sample

            ok = sum(1 for r in settled if r.get("ok"))
            failed = len(settled) - ok
            print("-" * 78)
            print(f"judge = {provider['name']} ({provider['model']})")
            print(f"  调用 {len(settled)} 次：成功 {ok} / 最终失败 {failed}"
                  f"   成功率 {ok / len(settled) * 100:.1f}%")
            print(f"  重试：发起 {stats['retries']} 次，其中挽回成功 {stats['retry_recovered']} 次"
                  f"  ⇒ **若不重试，预计失败 {failed + stats['retry_recovered']} 次**"
                  f"（{(failed + stats['retry_recovered']) / len(settled) * 100:.1f}%，推算值）")
            if stats["request"]:
                req = sorted(stats["request"])
                e2e = sorted(stats["e2e"])
                print(f"  单次 HTTP 往返：中位 {statistics.median(req):.2f}s  "
                      f"最快 {req[0]:.2f}s  最慢 {req[-1]:.2f}s")
                print(f"  含排队+退避的端到端：中位 {statistics.median(e2e):.2f}s  "
                      f"最慢 {e2e[-1]:.2f}s")
                print(f"  整轮墙钟（并发 {CONCURRENCY}）：{wall:.1f}s")
                # 自洽校验：若 request 口径正确，并发满载时 wall ≈ 总请求数 × 中位 ÷ 并发
                expect = len(settled) * statistics.median(req) / CONCURRENCY
                print(f"    口径自检：总调用 × 中位 ÷ 并发 = {expect:.1f}s，"
                      f"与墙钟 {wall:.1f}s 之比 = {expect / wall:.2f}"
                      f"（≈1 说明计量口径自洽；≫1 说明计时起点被排队污染）")
            if stats["errors"]:
                print(f"  最终失败样例（前 3 条）：")
                for e in stats["errors"][:3]:
                    print(f"    - {e[:110]}")

            if ok == 0:
                print("  ⚠ 该 judge 全部调用失败，跳过后续统计")
                print()
                continue

            # ---- 逐样本明细 ----
            print(f"\n  {'样本':6s} {'组别':11s} {'维度分数（每次）':44s} {'二值':6s}")
            print(f"  {'-' * 74}")
            for sample in SAMPLES:
                rows = by_sample[sample["id"]]
                good = [r for r in rows if r.get("ok")]
                if not good:
                    print(f"  {sample['id']:6s} {sample['group']:11s} (全部失败)")
                    continue
                corr = [r["correctness"] for r in good]
                faithful = [r["faithfulness"] for r in good]
                comp = [r["completeness"] for r in good]
                binary = [1 if c >= PASS_THRESHOLD else 0 for c in corr]
                mark = "翻转!" if len(set(binary)) > 1 else ("对" if binary[0] else "错")
                spread = max(corr) - min(corr)
                print(
                    f"  {sample['id']:6s} {sample['group']:11s} "
                    f"corr={self_fmt(corr)} faith={self_fmt(faithful)} comp={self_fmt(comp)} "
                    f"| 极差={spread} {mark}"
                )
            print()

            # ---- 分档分布（第 2 轮新增，是 ε 能否成立的**前置判据**）----
            # 第 1 轮 ε = 0 是个假数：judge 只动用 {0,2,3,5} 四档，阈值带（3.5~4.5）
            # 里一条样本都没有 —— 不翻转是**必然**的，跟 judge 稳不稳毫无关系。
            # 所以顺序必须是：先看分档覆盖了阈值附近没有，再看 ε。
            corr_all = [
                r["correctness"]
                for sample in SAMPLES
                for r in by_sample[sample["id"]]
                if r.get("ok")
            ]
            hist = {k: corr_all.count(k) for k in range(6)}
            print("  correctness 实际分档分布（全部成功调用）：")
            width = max(hist.values()) or 1     # 全 0 时兜底，避免除零
            for k in range(5, -1, -1):
                bar = "█" * round(hist[k] / width * 30)
                flag = "   ← 二值化阈值" if k == PASS_THRESHOLD else ""
                print(f"    {k} 分 │ {hist[k]:4d}  {bar}{flag}")
            band = hist[PASS_THRESHOLD] + hist[PASS_THRESHOLD - 1]
            ratio = band / len(corr_all) * 100 if corr_all else 0.0
            print(f"    阈值带（{PASS_THRESHOLD - 1} ~ {PASS_THRESHOLD} 分）占比 = "
                  f"{band}/{len(corr_all)} = {ratio:.1f}%")
            print(f"    全程空置的分档 = {[k for k in range(6) if hist[k] == 0] or '无'}")
            if band == 0:
                print("    ⚠ **阈值带为空** ⇒ 下面的 ε 必然为 0，这个 0 不能用来推噪声地板。")
            elif ratio < 20:
                print(f"    ⚠ 阈值带样本偏少（{ratio:.1f}%）⇒ ε 的估计**很不稳**，只能当下限参考。")
            else:
                print("    ✓ 阈值带有足够样本 ⇒ 本轮的 ε 才具备可用性。")
            print()

            # ---- 抖动 → ε ----
            all_runs = 0
            mismatch = 0
            flipped_samples = 0
            jitter_samples = 0          # 分数级抖动（出现过多个分档）
            scored_samples = 0
            for sample in SAMPLES:
                good = [r for r in by_sample[sample["id"]] if r.get("ok")]
                if not good:
                    continue
                scored_samples += 1
                binary = [1 if r["correctness"] >= PASS_THRESHOLD else 0 for r in good]
                truth = majority(binary)
                if len(set(binary)) > 1:
                    flipped_samples += 1
                if len({r["correctness"] for r in good}) > 1:
                    jitter_samples += 1
                all_runs += len(binary)
                mismatch += sum(1 for b in binary if b != truth)

            eps = mismatch / all_runs if all_runs else 0.0
            eps_lo, eps_hi = wilson_interval(mismatch, all_runs)

            print(f"  分数级抖动（同一份答案 {runs} 次内 correctness 出现过多个分档）："
                  f"{jitter_samples}/{scored_samples}")
            print(f"  二值翻转（这些抖动里真正跨过阈值的）："
                  f"{flipped_samples}/{scored_samples}")
            if flipped_samples:
                print(f"  ⇒ 分数级抖动样本数是二值翻转的 {jitter_samples / flipped_samples:.1f} 倍："
                      f"**只看翻转率会严重低估 judge 的不稳定性**")
            else:
                print(f"  ⇒ 二值翻转 0 条、但分数级抖动 {jitter_samples} 条："
                      f"**只看翻转率会把 judge 判成「完全稳定」，这是错的**")
            print(f"  单次判定与「{runs} 次多数投票真值」不一致：{mismatch}/{all_runs}"
                  f"  →  ε ≈ {eps:.4f}  (95% Wilson 区间 {eps_lo:.4f} ~ {eps_hi:.4f})")
            print(f"    ⚠ 偏差说明：真值取自同一批 {runs} 次，故 ε 被**略微低估**（乐观）")
            print(f"    ⚠ 外推限制：ε 是在这 {len(SAMPLES)} 份样本上量的。"
                  f"真实评测集的难度配比若与这批不同（更简单 → ε 更小），")
            print(f"      则下面的噪声地板会偏保守；反之偏乐观。**换评测集必须重测。**")

            # ---- ε → 噪声地板 ----
            # 每条题上，两配置"判定不同"的概率 = 一条错一条对 = 2ε(1-ε)
            p_diff = 2 * eps * (1 - eps)
            floor = EVAL_SET_SIZE * p_diff
            sd = (EVAL_SET_SIZE * p_diff * (1 - p_diff)) ** 0.5
            print()
            print(f"  ── 由 ε 推出噪声地板（消融规模 {EVAL_SET_SIZE} 条）──")
            print(f"  单条题两配置判定不同的概率 p = 2ε(1-ε) = {p_diff:.4f}")
            print(f"  纯噪声期望制造的差异条数 = {EVAL_SET_SIZE} × p = {floor:.2f} 条"
                  f"（σ = {sd:.2f}）")
            print(f"  ⇒ 观测到的差异若 **≤ {floor + 2 * sd:.1f} 条**，在 95% 水平上无法与噪声区分")
            print(f"  ⇒ 换算成百分比：{floor / EVAL_SET_SIZE * 100:.1f}% "
                  f"~ {(floor + 2 * sd) / EVAL_SET_SIZE * 100:.1f}%")
            print(f"  注：这是**噪声地板**，不是判据的全部 —— 真实结论还取决于方向是否一致"
                  f"（C 是否系统性优于 A，而非互有胜负）")
            print()

    # ============================================================
    # 跨 judge 交叉一致性
    # ============================================================
    names = [j["name"] for j in judges if j["name"] in results]
    if len(names) >= 2:
        a, b = names[0], names[1]
        print("=" * 78)
        print(f"跨 judge 交叉一致性：{a} vs {b}（同一份答案，多数投票结论是否一致）")
        print("=" * 78)
        agree = 0
        total = 0
        for sample in SAMPLES:
            ra = [r for r in results[a][sample["id"]] if r.get("ok")]
            rb = [r for r in results[b][sample["id"]] if r.get("ok")]
            if not ra or not rb:
                continue
            ta = majority([1 if r["correctness"] >= PASS_THRESHOLD else 0 for r in ra])
            tb = majority([1 if r["correctness"] >= PASS_THRESHOLD else 0 for r in rb])
            total += 1
            if ta == tb:
                agree += 1
            else:
                ma = statistics.mean([r["correctness"] for r in ra])
                mb = statistics.mean([r["correctness"] for r in rb])
                print(f"  分歧 {sample['id']:4s} {sample['group']:11s} "
                      f"{a} 判{'对' if ta else '错'}(corr均值 {ma:.1f}) / "
                      f"{b} 判{'对' if tb else '错'}(corr均值 {mb:.1f})")
        if total:
            lo, hi = wilson_interval(agree, total)
            print(f"\n  一致 {agree}/{total}  →  {agree / total * 100:.1f}%"
                  f"  (95% Wilson {lo * 100:.1f}% ~ {hi * 100:.1f}%)")
            print(f"  样本量只有 {total} 条，这个比例本身有很宽的不确定度 —— 只作方向参考，不当结论。")
        print()

    # ============================================================
    # 结论区（全部由上面的实测变量算出，无写死）
    # ============================================================
    print("=" * 78)
    print("结论（数据驱动，均取自本次实测）")
    print("=" * 78)
    print(f"· judge 数量 = {len(names)}，样本 {len(SAMPLES)} 份 × {runs} 次")
    print(f"· 单次判定的不一致率 ε 已在上面逐 judge 给出，噪声地板已换算成条数与百分比")
    print(f"· 判据：观测差异需 **同时满足** ①超过噪声地板 +2σ  ②方向一致（C 系统性优于 A）")
    sys.stdout.flush()


def self_fmt(values: list[int]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


def main() -> None:
    parser = argparse.ArgumentParser(description="D20 judge 抖动探针")
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--judges", type=str, default="deepseek,relay-gpt")
    args = parser.parse_args()
    asyncio.run(run(args.runs, [s.strip() for s in args.judges.split(",") if s.strip()]))


if __name__ == "__main__":
    main()
