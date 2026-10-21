"""
单元：画像抽取器的触发判断与输出容错
====================================
不连库、不连 Redis、不调真 LLM —— 全部替换掉。

为什么这个文件值得存在：
  抽取器是**后台任务**，它的失败不会被任何人当场看到（没有用户界面等它）。
  它安静地坏掉的表现是"画像永远不更新"，而用户只会觉得"这功能好像没用"。
  所以触发条件、冷却、失败后是否推进游标这几件事必须被钉住。

其中最关键的一条：**LLM 失败时要写冷却时间**。不写的话，一次模型故障会变成
"每一轮对话都再试一次失败调用"——把一次性故障放大成持续雪崩。
"""

from __future__ import annotations

import uuid

import pytest

from app.services import llm_gateway
from app.services import profile_extractor as pe
from app.services import profile_service as ps


def _rows(n: int) -> list[dict]:
    return [{"id": i + 1, "content": f"第 {i + 1} 句话", "session_id": None} for i in range(n)]


# ============================================================
# 输出解析（模型不听话是常态）
# ============================================================


def test_parse_plain_array():
    assert pe.parse_facts_json('[{"kind":"fact","text":"是前端工程师"}]') == [
        {"kind": "fact", "text": "是前端工程师"}
    ]


def test_parse_strips_markdown_fence():
    """prompt 明说了不要代码块，但模型照包不误 —— 不处理就等于每次都失败。"""
    raw = '```json\n[{"kind":"fact","text":"是前端工程师"}]\n```'
    assert pe.parse_facts_json(raw) == [{"kind": "fact", "text": "是前端工程师"}]


def test_parse_tolerates_surrounding_prose():
    raw = '好的，以下是提取结果：\n[{"kind":"fact","text":"x"}]\n希望有帮助。'
    assert pe.parse_facts_json(raw) == [{"kind": "fact", "text": "x"}]


def test_parse_wraps_single_object():
    """
    模型偶尔只抽到一条就忘了包数组。
    直接判 `isinstance(data, list)` 会让"只有一条事实"这个最常见的情况每次都失败。
    """
    assert pe.parse_facts_json('{"kind":"fact","text":"x"}') == [{"kind": "fact", "text": "x"}]


def test_parse_drops_items_without_text():
    assert pe.parse_facts_json('[{"kind":"fact"},{"text":"有内容"}]') == [{"text": "有内容"}]


def test_parse_empty_array():
    assert pe.parse_facts_json("[]") == []


def test_parse_raises_on_non_json():
    with pytest.raises(ValueError):
        pe.parse_facts_json("模型今天不想说话")


# ============================================================
# 触发判断
# ============================================================


async def test_no_owner_skips():
    """系统调用 / 评测没有归属 → 不抽（否则会给"无主"建一份画像）。"""
    result = await pe.maybe_extract(None, None)
    assert result == {"extracted": False, "reason": "no_owner", "added": 0}


async def test_below_trigger(monkeypatch):
    async def fake_collect(*_a, **_k):
        return _rows(3)

    monkeypatch.setattr(pe, "_get_cursor", _async_return(None))
    monkeypatch.setattr(pe, "collect_new_user_messages", fake_collect)

    result = await pe.maybe_extract(uuid.uuid4(), None)
    assert result["reason"] == "below_trigger"
    assert result["pending"] == 3


async def test_cooldown_blocks_second_run(monkeypatch):
    import time

    async def fake_collect(*_a, **_k):
        return _rows(6)

    monkeypatch.setattr(pe, "_get_cursor", _async_return(None))
    monkeypatch.setattr(pe, "_get_last_at", _async_return(time.time()))
    monkeypatch.setattr(pe, "collect_new_user_messages", fake_collect)

    result = await pe.maybe_extract(uuid.uuid4(), None)
    assert result["reason"] == "cooldown"


# ============================================================
# 抽取行为（LLM 用替身）
# ============================================================


async def test_llm_failure_sets_cooldown(monkeypatch):
    """
    LLM 挂了：不推进游标，但**必须**写冷却时间。

    不写冷却 → 下一轮对话立刻再试一次 → 一次故障被放大成"每轮一次失败调用"。
    """
    touched: list[float] = []

    async def fake_set_last_at(_owner, ts):
        touched.append(ts)

    async def boom(*_a, **_k):
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(pe, "_set_last_at", fake_set_last_at)
    monkeypatch.setattr(ps, "list_facts", _async_return([]))
    monkeypatch.setattr(llm_gateway, "chat", boom)

    result = await pe._extract(uuid.uuid4(), None, _rows(6))
    assert result == {"extracted": False, "reason": "llm_failed", "added": 0}
    assert len(touched) == 1


async def test_bad_json_sets_cooldown(monkeypatch):
    touched: list[float] = []

    async def fake_set_last_at(_owner, ts):
        touched.append(ts)

    async def garbage(*_a, **_k):
        return "我觉得这个用户挺好的"

    monkeypatch.setattr(pe, "_set_last_at", fake_set_last_at)
    monkeypatch.setattr(ps, "list_facts", _async_return([]))
    monkeypatch.setattr(llm_gateway, "chat", garbage)

    result = await pe._extract(uuid.uuid4(), None, _rows(6))
    assert result["reason"] == "bad_json"
    assert len(touched) == 1


async def test_success_advances_cursor(monkeypatch):
    """成功时：写入事实、把游标推进到本批最后一条、并记冷却时间。"""
    cursor_written: list[int] = []
    captured: list[dict] = []

    async def fake_chat(*_a, **_k):
        return '[{"kind":"fact","text":"是前端工程师","confidence":0.9}]'

    async def fake_add_facts(_owner, facts, **_kw):
        captured.extend(facts)
        return len(facts)

    async def fake_set_cursor(_owner, message_id):
        cursor_written.append(message_id)

    monkeypatch.setattr(pe, "_set_last_at", _async_return(None))
    monkeypatch.setattr(pe, "_set_cursor", fake_set_cursor)
    monkeypatch.setattr(ps, "list_facts", _async_return([]))
    monkeypatch.setattr(ps, "add_facts", fake_add_facts)
    monkeypatch.setattr(llm_gateway, "chat", fake_chat)

    rows = _rows(6)
    result = await pe._extract(uuid.uuid4(), None, rows)

    assert result["extracted"] is True
    assert result["added"] == 1
    assert cursor_written == [rows[-1]["id"]]
    assert captured[0]["text"] == "是前端工程师"


# ============================================================
# 后台调度
# ============================================================


def test_schedule_without_owner_returns_false():
    assert pe.schedule_extract(None, None) is False


# ============================================================
# 小工具
# ============================================================


def _async_return(value):
    """造一个"总是返回 value"的 async 函数（用来替换 Redis 读写）。"""

    async def _inner(*_a, **_k):
        return value

    return _inner
