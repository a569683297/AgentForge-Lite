"""
单元：Agent 图的决策逻辑（D6 LangGraph，D14 合并 plan/answer）
==============================================================
图本身跑起来要 LLM + DB，但那只是"装配"；
**决策规则**（走 execute 还是收工）与**提示词组装**是纯逻辑 —— 出错时症状很远
（多烧一次 LLM、或摘要段凭空出现一条空的），所以单独钉住。
"""

from app.services.agent_service import RECURSION_LIMIT, build_system_prompt, should_continue


def _state(messages: list[dict]) -> dict:
    return {"messages": messages, "step_count": 0, "summary": None}


def test_tool_calls_present_leads_to_execute():
    state = _state([{"role": "assistant", "tool_calls": [{"id": "call_1"}]}])
    assert should_continue(state) == "execute"


def test_no_tool_calls_ends_the_graph():
    state = _state([{"role": "assistant", "content": "直接回答"}])
    assert should_continue(state) == "end"


def test_empty_tool_call_list_still_ends():
    """
    空列表是 **falsy** —— 判断必须看"有没有内容"而不是"字段在不在"。
    否则会走一个什么都不做的 execute 节点，白烧一次循环配额。
    """
    state = _state([{"role": "assistant", "content": "无工具", "tool_calls": []}])
    assert should_continue(state) == "end"


def test_recursion_limit_is_twelve():
    """
    PRD v4.1 §9.2 定为 12（4 轮下钻 = 9 节点 + 3 余量）。

    ⚠ 这条断言存在的理由：修复前这个常量**只写在注释里**，代码从未把它传给
      LangGraph，实际生效的是库默认值 25 —— 注释与行为不符，而且看不出来。
      钉住它 = 防止将来又退化成"注释说了、代码没做"。
    """
    assert RECURSION_LIMIT == 12


def test_system_prompt_without_summary():
    prompt = build_system_prompt(None)
    assert prompt  # 非空


def test_system_prompt_with_summary_is_longer_and_contains_it():
    """
    有历史摘要时，提示词必须**真的把摘要内容带进去**。
    "长度变长" + "内容在其中"两条一起断言：只测长度会被固定文案蒙过去，
    只测内容又测不出它是不是被放在了 system 里。
    """
    base = build_system_prompt(None)
    with_summary = build_system_prompt("用户之前问过年假天数")

    assert len(with_summary) > len(base)
    assert "用户之前问过年假天数" in with_summary


def test_empty_summary_is_treated_as_no_summary():
    """空串不该产生一个"有摘要但摘要为空"的段落（那个段落对 LLM 只是噪声）。"""
    assert build_system_prompt("") == build_system_prompt(None)


# ---- 用户画像注入（2026-10-21）----
# 这三条守的是"画像到底有没有进 prompt、进在什么位置"。
# 位置是有讲究的：人设 → 画像 → 摘要（稳定背景在临时上下文之前）。


def test_profile_block_is_injected():
    prompt = build_system_prompt(None, "【用户档案】\n- 称呼：老郭")
    assert "称呼：老郭" in prompt


def test_profile_comes_before_summary():
    """
    画像必须排在摘要**之前**。

    画像是"你是谁"（跨会话稳定），摘要是"刚才聊了什么"（本会话临时）。
    顺序反了，模型更容易用一段临时上下文盖掉长期设定 ——
    这种漂移不会报错，只会让画像时灵时不灵。
    """
    prompt = build_system_prompt("更早的对话摘要", "【用户档案】\n- 称呼：老郭")
    assert prompt.index("称呼：老郭") < prompt.index("更早的对话摘要")


def test_empty_profile_adds_nothing():
    """没有画像时，prompt 与"只有摘要"的形状完全一致（不能多出一个空段）。"""
    assert build_system_prompt("摘要", "") == build_system_prompt("摘要")
    assert build_system_prompt("摘要", None) == build_system_prompt("摘要")
