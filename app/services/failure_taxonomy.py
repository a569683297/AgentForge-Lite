"""
失败归因（D22）—— 纯函数推导规则表
====================================
把「这道题为什么没过」从**主观描述**变成**可复算的推导**。

--------------------------------------------------------------------------
为什么不能让 judge 直接吐一个 failure_reason 字段（最省的写法）
--------------------------------------------------------------------------
三个理由，每一个单独都足以否决这条路：

    ① 同义异形 —— 5 次调用会写出「检索未命中」/「资料缺失」/「没找到相关内容」
       /「知识库无此信息」。按字符串聚类会被算成 **4 类**，
       报告里的"失败模式分布"于是是**假的**，而且没有任何报错。
    ② 不可复现 —— 同一份答案重跑一次，judge 换个措辞 → 同一份报告两次不一样。
       （D20 已实测 judge 有抖动；自由文本把抖动从分数放大到了分类。）
    ③ 不可测 —— 自由文本没法写断言。"这条应该是 hallucination" 无法机检，
       于是这份规则表永远是"看起来对"而不是"验证过对"。

同族先例（本项目真发生过）：D14 的 `messages.tool_calls` 被 SQLAlchemy 存成
JSON 字面量 `null` 而非 SQL NULL —— **同一语义有两种表示**，
`IS NULL` 恒为假，整片逻辑静默失效。

--------------------------------------------------------------------------
判据一句话
--------------------------------------------------------------------------
    **能用纯函数从已有数字推出来的东西，不要让模型再生成一遍。**

judge 的不可替代之处是"语义判断"（这段文字与那段文字意思一样吗），
那部分它输出**数字分数**；"这个数字组合意味着哪种失败"是同义反复，
必须由代码算 —— 这样它可单测、可复算、可以拿断言钉住。

可执行自检（d22_verify.py 的 D 段在做）：
    同一份答案重复打分 10 次，**推导结果必须完全相同**。
    只要出现两次不同，就说明有一条规则依赖了分数以外的状态。
"""

from app.models.eval import (
    EXPECTED_TOOL_NOT_CALLED,
    FAILURE_HALLUCINATION,
    FAILURE_INCOMPLETE,
    FAILURE_SYSTEM_ERROR,
    FAILURE_TOOL_MISS,
    FAILURE_WRONG_ANSWER,
    PASS_THRESHOLD,
)

# ============================================================
# 判据阈值（改这里 = 改判据，必须整体重跑评测）
# ============================================================
# 为什么是 ≤ 2 而不是 ≤ 3：
#   rubric 里 3 分是"结论对但漏了限定条件"、2 分才是"关键事实有错"。
#   把 3 分也算成编造，会把"答得不完整"错误地归类成"幻觉" ——
#   而这两种失败的修法完全不同（一个改 prompt，一个改检索）。
HALLUCINATION_MAX_FAITHFULNESS = 2
INCOMPLETE_MAX_COMPLETENESS = 2

# 归因的中文标签（报告与前端展示用）。放这里而不是模板里：
# 枚举值与它的解释必须同处一地，否则加了新值忘了加标签，展示层会印出英文常量。
FAILURE_REASON_LABELS: dict[str, str] = {
    FAILURE_SYSTEM_ERROR: "系统未跑通",
    FAILURE_TOOL_MISS: "工具未调对",
    FAILURE_HALLUCINATION: "编造内容",
    FAILURE_INCOMPLETE: "回答不完整",
    FAILURE_WRONG_ANSWER: "答案错误",
}


# ============================================================
# 工具调用判定
# ============================================================
def tool_call_ok(expected_tool: str | None, actual_tools: list[str] | None) -> bool:
    """
    实际调用的工具是否满足题目要求。

    三种情况，语义各不相同（这就是 `__none__` 哨兵存在的原因）：

        expected_tool is None          → 题目**未约束**工具（A/B 类题），永远算满足。
                                         注意这与"要求不调工具"是两回事。
        expected_tool == "__none__"    → **显式要求不调用任何工具**（C08/C09/C10
                                         这类闲聊/算术题）。调了就是错 ——
                                         本可以直答却去查库，是能力误用。
        expected_tool == "xxx"         → 必须调用到它。**调了别的也算不满足**
                                         （expected not in actual）：
                                         C 类题测的是"选对工具"，不是"有调工具"。

    ⚠ 用 in 而不是 == 比较：一步题里可能先调 search_documents 再调别的，
      只要目标工具在序列里出现过就算命中（调用顺序的合理性是另一码事，
      要等 D 类多步题落地才有数据可判，见 PRD §17）。
    """
    if expected_tool is None:
        return True
    actual = actual_tools or []
    if expected_tool == EXPECTED_TOOL_NOT_CALLED:
        return len(actual) == 0
    return expected_tool in actual


# ============================================================
# 主推导函数
# ============================================================
def derive_case_outcome(
    *,
    correctness: float | None,
    faithfulness: float | None,
    completeness: float | None,
    expected_tool: str | None = None,
    actual_tools: list[str] | None = None,
    run_failed: bool = False,
) -> tuple[bool, str | None]:
    """
    由分数与工具调用序列推导出 (是否通过, 失败原因)。

    返回二元组而不是只返回原因：**"通过"与"失败原因"是同一个判断的两面**，
    分成两个函数写会立刻出现"passed=True 却有 failure_reason"这种不自洽状态。

    ----------------------------------------------------------------------
    优先级：上游原因优先于下游表现
    ----------------------------------------------------------------------
    "没调工具"是**根因**，"答错了"是它的**结果**。若一律记成 wrong_answer，
    "工具选择错误"这个失败模式就被藏起来了 —— 而它恰恰是 C 类题存在的意义
    （C 类题的全部价值就是测"会不会选对工具"）。

    顺序（= FAILURE_REASONS 的顺序）：
        system_error → tool_miss → hallucination → incomplete → wrong_answer

    ----------------------------------------------------------------------
    两个刻意的设计决定
    ----------------------------------------------------------------------
    ① **C 类题 tool_miss 有否决权**：答题侥幸对了也算不通过。
       C 类题测的是工具调用能力，答案对不是它要测的东西。
       这样调用方能在报告里区分"答错"和"没调工具"两种失败。
    ② **先判 faithfulness 再判 completeness**：编造比漏答严重。
       编造是"引入了不存在的事实"（会误导用户）；漏答只是"说得不全"。
    """
    if run_failed:
        # 连答案都没跑出来。分数此时必然是 None，先返回，避免下面拿 None 去比大小。
        return False, FAILURE_SYSTEM_ERROR

    # 分数缺失 = 调用方用错了。**抛错而不是当成 0 分**：
    # 当成 0 分会让"打分器坏了"伪装成"系统答错了"，两种问题的修法完全不同。
    # 这正是 D21 那条教训的形态：静默地把一种情况算成另一种。
    if correctness is None or faithfulness is None or completeness is None:
        raise ValueError(
            "derive_case_outcome 需要三个维度都有分数；"
            f"收到 correctness={correctness} faithfulness={faithfulness} "
            f"completeness={completeness}。"
            "（打分完全失败的行不应进入本函数 —— 请在上游拦成异常，"
            "不要让它落库成一条'答错'）"
        )

    # ① 工具是否调对（C 类题的否决权，排在分数之前）
    if not tool_call_ok(expected_tool, actual_tools):
        return False, FAILURE_TOOL_MISS

    # ② 分数是否达标（PRD F7.5：correctness ≥ 4 记对）
    if correctness >= PASS_THRESHOLD:
        return True, None

    # ③ 未达标 → 归因到具体失败模式（优先级见上）
    if faithfulness <= HALLUCINATION_MAX_FAITHFULNESS:
        return False, FAILURE_HALLUCINATION
    if completeness <= INCOMPLETE_MAX_COMPLETENESS:
        return False, FAILURE_INCOMPLETE
    return False, FAILURE_WRONG_ANSWER


def describe(reason: str | None) -> str:
    """失败原因的中文标签（报告用）。未知值原样返回 —— 不掩盖枚举外的值。"""
    if reason is None:
        return "通过"
    return FAILURE_REASON_LABELS.get(reason, reason)
