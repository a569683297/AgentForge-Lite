"""
单元：切片器 `split_text`（D9 引入，D17 复用）
==============================================
为什么盯它：切片是整条 RAG 链路的**第一道几何约束** ——
切片切坏了，后面的向量化、BM25、重排再准也救不回来。
而它是个纯函数（只吃字符串），是最好写"边界证据"的地方。
"""

import pytest

from app.services.retrieval_service import CHUNK_OVERLAP, CHUNK_SIZE, split_text


def test_overlap_must_be_smaller_than_chunk_size():
    """
    步长 = chunk_size - overlap。若 overlap >= chunk_size，步长 <= 0 →
    `range(0, len, 0)` 当场 ValueError、负数则死循环。必须在入口拦住。
    """
    with pytest.raises(ValueError, match="overlap"):
        split_text("x" * 500, chunk_size=100, overlap=100)
    with pytest.raises(ValueError, match="overlap"):
        split_text("x" * 500, chunk_size=100, overlap=200)


def test_short_text_returns_single_chunk():
    """短于一片的文本不该被"切"，原样返回一片。"""
    assert split_text("很短的一句话") == ["很短的一句话"]


def test_blank_text_returns_empty_list():
    """空白不算内容：返回 []，而不是 ['']（后者会让下游多出一条空切片）。"""
    assert split_text("   \n\t  ") == []
    assert split_text("") == []


def test_long_text_is_split_into_expected_count():
    """700 字 / (300 - 50) = 步长 250 → 起点 0 / 250 / 500，共 3 片。"""
    chunks = split_text("a" * 700, chunk_size=300, overlap=50)
    assert len(chunks) == 3


def test_adjacent_chunks_overlap_by_configured_amount():
    """
    重叠的**目的**是别把一句话从中间切断，所以"相邻两片确实重叠了 overlap 个字"
    才是这个参数的意义所在 —— 只断言"切片数对"是测不出这个的。
    """
    text = "".join(chr(0x4E00 + i) for i in range(700))  # 700 个互不相同的汉字
    chunks = split_text(text, chunk_size=300, overlap=50)
    for left, right in zip(chunks, chunks[1:], strict=False):
        assert left.endswith(right[:50]), "相邻切片必须有 50 字重叠"


def test_no_character_is_dropped():
    """
    ★ 覆盖面断言：原文的每一个字符都必须出现在某个切片里。

    为什么用"互不相同的汉字"而不是 "aaaa"：字符重复的话 `in` 判断必然成立，
    这条断言就变成恒真的了 —— 写了等于没写。
    """
    text = "".join(chr(0x4E00 + i) for i in range(1000))
    chunks = split_text(text)
    joined = "".join(chunks)

    missing = [ch for ch in text if ch not in joined]
    assert missing == [], f"有 {len(missing)} 个字被切片丢了"


def test_total_length_is_not_shorter_than_source():
    """重叠会让总长**变长**（重复是设计），但绝不允许变短（变短 = 丢字）。"""
    text = "b" * 1000
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert sum(len(c) for c in chunks) >= len(text)


def test_surrounding_whitespace_is_stripped():
    """首尾空白被去掉 —— 否则它会白白占掉切片配额。"""
    text = "  " + "b" * 400 + "  "
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert not chunks[0].startswith(" ")
    assert not chunks[-1].endswith(" ")


def test_defaults_match_module_constants():
    """默认参数就是模块常量本身（防止有人改了一处忘改另一处）。"""
    text = "c" * (CHUNK_SIZE * 2)
    chunks = split_text(text)
    assert all(len(c) <= CHUNK_SIZE for c in chunks)
    assert len(chunks) >= 2
    assert CHUNK_OVERLAP < CHUNK_SIZE
