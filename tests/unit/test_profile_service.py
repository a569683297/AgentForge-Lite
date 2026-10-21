"""
单元：画像的规范化、去重依据、top-k 匹配与渲染
==============================================
这个文件守的是**纯函数**那一半 —— 不连库、不连 Redis、不调 LLM。

为什么这几件小事值得单独测：
  · **规范化**决定了去重成不成立。抽一句 "偏好 TypeScript。"、下次抽 "偏好 TypeScript"，
    规范化少做一步就会在库里变成两条 —— 而 top-k 只有 4 个配额。
  · **匹配**决定了"画像到底有没有被用上"。匹配错了不会报错，
    只是 Agent 答得不像认识你 —— 那是这类功能最典型的静默失败。
  · **渲染**里那句"不要在回答里主动复述"是行为约束，删掉不会有测试变红，
    但模型会开始说"我记得你……"。
"""

from __future__ import annotations

import pytest

from app.services import profile_service as ps


def _fact(fid: int = 1, text: str = "", kind: str = "fact", conf: float = 0.8) -> ps.FactItem:
    return ps.FactItem(
        id=fid,
        kind=kind,
        text=text,
        confidence=conf,
        source_session_id=None,
        created_at=None,
    )


# ============================================================
# 规范化与哈希
# ============================================================


def test_normalize_trims_and_drops_trailing_punct():
    assert ps.normalize_fact_text("  偏好 TypeScript。 ") == "偏好 TypeScript"
    assert ps.normalize_fact_text("正在准备面试！！") == "正在准备面试"


def test_normalize_fullwidth_to_halfwidth():
    # NFKC：全角字母/数字转半角。不做这一步，"Ｔｙｐｅ" 与 "Type" 会是两条不同的事实。
    assert ps.normalize_fact_text("ＴｙｐｅＳｃｒｉｐｔ") == "TypeScript"
    assert ps.normalize_fact_text("０．９") == "0.9"


def test_normalize_collapses_inner_whitespace():
    assert ps.normalize_fact_text("偏好   TypeScript\t和\nReact") == "偏好 TypeScript 和 React"


def test_normalize_empty_is_empty():
    assert ps.normalize_fact_text("") == ""
    assert ps.normalize_fact_text("   \n\t ") == ""
    assert ps.normalize_fact_text("。。。") == ""


def test_hash_is_stable_across_trivial_wording_differences():
    """去重的地基：只差标点/空白/全角的两个串，必须得到同一个 hash。"""
    base = ps.fact_hash("偏好 TypeScript 而不是 JavaScript")
    assert ps.fact_hash("偏好 TypeScript 而不是 JavaScript。") == base
    assert ps.fact_hash("  偏好 TypeScript 而不是 JavaScript  ") == base
    assert ps.fact_hash("偏好 TypeScript 而不是 JavaScript！") == base


def test_hash_differs_for_different_text():
    assert ps.fact_hash("A") != ps.fact_hash("B")
    assert len(ps.fact_hash("任意内容")) == 64


# ============================================================
# top-k 匹配
# ============================================================


def test_relevant_fact_ranks_first():
    facts = [
        _fact(1, "在电商公司做前端", conf=0.95),
        _fact(2, "主要写 TypeScript 和 React", conf=0.7),
    ]
    result = ps.select_relevant(facts, "React 的 useEffect 怎么写")
    assert result[0].id == 2


def test_falls_back_to_confidence_order_when_nothing_matches():
    """
    一条都不相关时，按 confidence 降序取满 top_k（不是只取 1 条）。

    为什么不返回空列表：用户的很多提问（"帮我看下这段代码"）与画像字面毫无重合，
    但"他是前端工程师、要直接给结论"仍然该影响回答。完全不带任何事实，
    会让画像在**最需要它的时候（泛泛的提问）恰好消失**。

    为什么取满而不是只取 1 条：零相关只说明匹配没给出信号，
    不代表这些事实都同样无关 —— 见下面那条故障回归用例。
    """
    facts = [
        _fact(1, "偏好 TypeScript", conf=0.6),
        _fact(2, "正在准备面试", conf=0.9),
    ]
    result = ps.select_relevant(facts, "今天天气怎么样")
    assert [f.id for f in result] == [2, 1]


def test_name_fact_survives_zero_relevance_query():
    """
    故障回归（2026-10-21）：两条同类事实时，"我叫什么"必须能拿到称呼那条。

    现场：事实是「是开发工程师」「希望被称呼为梦」，confidence 都是 0.95；
    查询「我叫什么」分词为 ['我','叫','什么']，两条余弦都是 0。
    旧实现兜底只取 1 条 → 按 id 取到「是开发工程师」→ 名字被丢掉 →
    Agent 回答"我没有你的名字信息"。用户看到的就是"画像没有被召回"。
    """
    facts = [
        _fact(49, "是开发工程师", conf=0.95),
        _fact(50, "希望被称呼为梦", conf=0.95),
    ]
    texts = [f.text for f in ps.select_relevant(facts, "我叫什么？")]
    assert "希望被称呼为梦" in texts, texts


def test_fallback_never_exceeds_top_k():
    """兜底也要守住预算：事实很多时不能整份塞进 prompt。"""
    facts = [_fact(i, f"第 {i} 条事实", conf=0.5) for i in range(1, 11)]
    assert len(ps.select_relevant(facts, "今天天气", top_k=3)) == 3


def test_respects_top_k():
    facts = [_fact(i, f"前端开发相关的第 {i} 条事实") for i in range(6)]
    result = ps.select_relevant(facts, "前端", top_k=2)
    assert len(result) == 2


def test_empty_facts_returns_empty():
    assert ps.select_relevant([], "任何问题") == []
    assert ps.select_relevant([_fact(1, "x")], "问题", top_k=0) == []


def test_empty_query_falls_back_to_confidence_order():
    """空查询 = 没有匹配依据 → 与零相关同规则（按 confidence 取满）。"""
    facts = [_fact(1, "A", conf=0.3), _fact(2, "B", conf=0.8)]
    assert [f.id for f in ps.select_relevant(facts, "   ")] == [2, 1]


# ============================================================
# 显式字段校验
# ============================================================


def test_validate_rejects_unknown_key():
    """
    未知键必须**拒绝**而不是忽略。

    忽略的后果是"保存成功但没生效"：前端把 nickname 拼成 nick_name，
    接口一直 200，用户反复保存都改不动 —— 这种问题几乎无法从反馈里定位。
    """
    with pytest.raises(ps.ProfileValidationError):
        ps.validate_explicit({"hobby": "钓鱼"})


def test_validate_rejects_too_long():
    limit = ps.EXPLICIT_VALUE_MAX["occupation"]
    with pytest.raises(ps.ProfileValidationError):
        ps.validate_explicit({"occupation": "x" * (limit + 1)})


def test_validate_drops_blank_values():
    """空值 = 删除这个字段（不存空串）—— 空串会让渲染出现"职业："这样的空行。"""
    assert ps.validate_explicit({"nickname": "   ", "occupation": "前端"}) == {
        "occupation": "前端"
    }


def test_validate_strips_values():
    assert ps.validate_explicit({"nickname": "  老郭  "}) == {"nickname": "老郭"}


# ============================================================
# 渲染
# ============================================================


def test_render_none_when_empty():
    assert ps.render_profile({}, []) is None
    assert ps.build_profile_block(ps.Profile(), "问题") is None


def test_render_explicit_before_facts():
    text = ps.render_profile({"nickname": "老郭"}, [_fact(1, "偏好 TypeScript")])
    assert text is not None
    assert text.index("【用户档案】") < text.index("【过往对话中了解到的信息】")


def test_render_keeps_guardrail():
    """
    那句"不要在回答里主动复述"是**行为约束**，不是润色。

    删掉的话，模型会开始说"记得你上次提到……"，而画像里根本没有"上次"的时序 ——
    它只是在复述一段背景文本，听起来像编造（实际是提示词诱导出来的行为）。
    """
    text = ps.render_profile({"nickname": "老郭"}, [])
    assert text is not None
    assert "不要在回答里主动复述" in text
    assert "以当前说法为准" in text


def test_render_skips_missing_explicit_fields():
    """只渲染有值的字段（不能出现"技术栈："这种空行）。"""
    text = ps.render_profile({"nickname": "老郭"}, [])
    assert text is not None
    assert "称呼：老郭" in text
    assert "技术栈" not in text


def test_render_only_facts():
    text = ps.render_profile({}, [_fact(1, "正在准备面试")])
    assert text is not None
    # ⚠ 断言要用**完整段落标题**，不能只写 "【用户档案】"：
    #   渲染末尾的优先级规则里也提到了这个词（"【用户档案】是用户本人填写的…"），
    #   用短串断言会误报 —— 第一版就是这么红的。断言要挑"只有目标段落才有"的特征。
    assert "【用户档案】（用户本人填写）" not in text
    assert "正在准备面试" in text
