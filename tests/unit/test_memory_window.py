"""
单元：记忆窗口的键、游标与配置契约（D4 滑窗 / D14 归一化 / D15 摘要）
========================================================================
这条链路的**状态全在 Redis 键名与几个配置常量里** —— 它们错了会以
"会话串味""摘要重复摘一遍""热窗口永远读不到"这类很难查的方式暴露，
所以先在最便宜的地方（纯函数）把它们钉住。
"""

import uuid

from app.services.memory_service import (
    MAX_MESSAGES,
    SUMMARY_BATCH_MESSAGES,
    SUMMARY_CURSOR_TTL,
    SUMMARY_MIN_MESSAGES,
    SUMMARY_TEMPERATURE,
    SUMMARY_TRIGGER_TURNS,
    WINDOW_TTL,
    WINDOW_TURNS,
    _format_dialogue,
    _summary_cursor_key,
    _to_uuid,
    _window_key,
)

# ============================================================
# 一、键名（命名空间）
# ============================================================


def test_keys_are_namespaced_by_session():
    session_id = uuid.uuid4()
    assert str(session_id) in _window_key(session_id)
    assert str(session_id) in _summary_cursor_key(session_id)


def test_window_and_cursor_keys_never_collide():
    """
    ★ 两个键必须是不同的字符串。

    撞了会怎样：游标（一个整数）会把整个对话窗口覆盖掉 ——
    症状是"历史突然全没了"，而下一次写窗口又会把游标冲掉 →
    摘要永远从 0 重摘。两个毛病互相掩盖，最难查。
    """
    session_id = uuid.uuid4()
    assert _window_key(session_id) != _summary_cursor_key(session_id)


def test_key_is_stable_for_the_same_session():
    """同一个会话必须每次算出同一个 key（否则每一轮都开一个新窗口）。"""
    session_id = uuid.uuid4()
    assert _window_key(session_id) == _window_key(session_id)


def test_keys_differ_across_sessions():
    """不同会话绝不能共用 key（那就是"会话串味"）。"""
    assert _window_key(uuid.uuid4()) != _window_key(uuid.uuid4())


# ============================================================
# 二、id 归一化（D14 踩过的坑）
# ============================================================


def test_to_uuid_accepts_uuid_and_its_string_forms():
    """
    ★ 为什么要归一化：PG 的 uuid 类型会把自己看到的值规范成**带横杠**的形态，
    而 D14 之前服务端生成的是 `uuid4().hex`（32 位无横杠）。
    两者拼出来的 Redis key 不是同一个字符串 ——
    于是"从 /api/sessions 拿到 id 再回来对话"会读不到热窗口，
    **静默**退化成走 PG 兜底（功能看着正常，只是一直在走慢路径）。
    """
    session_id = uuid.uuid4()
    assert _to_uuid(session_id) == session_id
    assert _to_uuid(str(session_id)) == session_id
    assert _to_uuid(session_id.hex) == session_id  # 32 位无横杠
    assert _to_uuid(str(session_id).upper()) == session_id  # 大写


def test_to_uuid_returns_none_for_non_uuid_ids():
    """
    非 uuid 形态返回 **None**（调用方据此跳过 PG 兜底），而不是抛异常。
    理由：本模块的历史调用方用过普通字符串 id（如 D8 的纯 Redis 用例），
    对那种 id 而言 PG 里不可能有对应会话，兜底无从谈起。
    """
    assert _to_uuid("d8-纯字符串会话") is None
    assert _to_uuid("") is None
    assert _to_uuid("12345") is None


# ============================================================
# 三、配置契约（PRD 上的数字）
# ============================================================


def test_window_is_eight_turns():
    """PRD F5.1：只保留最近 8 轮。每轮 = user + assistant = 2 条消息。"""
    assert WINDOW_TURNS == 8
    assert MAX_MESSAGES == WINDOW_TURNS * 2


def test_window_ttl_is_24_hours():
    """
    PRD F5.3：热窗口 24 小时过期。
    它能这么短的前提是 **PG 是全量兜底** —— 过期不等于丢失。
    """
    assert WINDOW_TTL == 86400


def test_cursor_ttl_outlives_window_ttl():
    """
    游标 TTL 必须**长于**窗口 TTL：窗口过期重建后，游标还在，
    才能"接着上次摘过的地方继续摘"，而不是把已摘的段落再摘一遍。
    """
    assert SUMMARY_CURSOR_TTL > WINDOW_TTL


def test_summary_trigger_matches_prd():
    """PRD F5.2：累计超过 20 轮才启动摘要压缩。"""
    assert SUMMARY_TRIGGER_TURNS == 20
    assert SUMMARY_MIN_MESSAGES >= 4, "至少攒够 2 轮才值得摘一次"
    assert SUMMARY_BATCH_MESSAGES >= SUMMARY_MIN_MESSAGES


def test_summary_temperature_is_the_faithful_one():
    """
    PRD §8.4 场景参数：摘要用 **0.1**。

    ⚠ 这不是"随便设小一点"：摘要要的是**忠实压缩**。
      用回答档的 0.4 会让它加戏、改事实 —— D14 把 temperature 从硬编码
      改成可传参数，就是为了这一个场景（此前摘要被迫共用 0.4）。
    """
    assert SUMMARY_TEMPERATURE == 0.1


# ============================================================
# 四、对话格式化
# ============================================================


def test_format_dialogue_labels_both_roles():
    rows = [
        {"role": "user", "content": "年假几天"},
        {"role": "assistant", "content": "5 天"},
    ]
    text = _format_dialogue(rows)
    assert "年假几天" in text and "5 天" in text
    assert "用户" in text and "助手" in text


def test_format_dialogue_on_empty_input():
    """空输入返回空串（调用方据此跳过"摘一段空的"）。"""
    assert _format_dialogue([]) == ""
