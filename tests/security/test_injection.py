"""
安全：注入与参数边界（PRD §14 安全层 / S13）
=============================================
PRD 的安全层点名三件事：① SQL 注入用例全拦截 ② 写操作确认门 ③ 凭据不外泄。
（②③ 在 `test_credentials.py`。）

⚠ **一处必须说清的覆盖面边界**：
  PRD ① 写的是"维度 / 指标名 / 时间参数"—— 那三类参数的注入面属于
  **语义层 `metrics_service`**，而语义层是 **D35** 的交付物，今天还不存在。
  所以那三类用例**现在没有被测对象**，不是"已通过"，是"还没得测"。

  今天能测的，是**当前真实存在的注入面**：用户问题进入 tsquery 的那条路
  （`tokenize` → `build_or_tsquery` → 参数化查询）。
  语义层落地后必须回来补写这三类 —— 待办已登记在文件末尾。
"""

import re

import pytest

from app.services.tokenizer import _clean_token, build_or_tsquery, tokenize

# 「实义字符」= 数字 / 拉丁字母 / 汉字 / 日文假名。
# 它与 `_HAS_WORD` 是同一份定义 —— 也就是 `_clean_token` 用来判定「这个词有没有意义」的那条规则。
META_CHAR_FREE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff]+$")

INJECTION_PAYLOADS = [
    "'; DROP TABLE messages; --",
    "' OR 1=1 --",
    "a&b|c!d(e)<f>:g*h",
    "'; DELETE FROM document_chunks WHERE 1=1; --",
    "$$ $$ || pg_sleep(10) --",
    "管理员' UNION SELECT * FROM sessions --",
    "年假<->制度",
    "*:*",
]


# ============================================================
# 一、核心不变量（纯函数，不需要数据库）
# ============================================================


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_tokenize_output_contains_only_meaningful_characters(payload):
    """
    ★ 这是本文件最重要的一条：**tokenize 的每个输出 token 只能由实义字符组成**。

    为什么这条比"断言不含 `;` 和 `'`"更强：
      后者是**枚举黑名单** —— 漏一个元字符就漏一个洞；
      前者是**结构不变量**（由 `_clean_token` 的剥除正则 + 实义判定共同保证），
      任何元字符（包括将来 PostgreSQL 新增的、我没想到的）都进不来。
    """
    for token in tokenize(payload):
        assert META_CHAR_FREE.match(token), f"token 里混进了非实义字符：{token!r}"


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_tsquery_pieces_are_all_plain_words(payload):
    """
    拼出来的 tsquery 拆开看，每一段都必须是纯词 —— 段与段之间的 ` | `
    分隔符是**我们自己插的**，不是用户输入里来的。

    这条把"注入"的定义钉死了：如果用户能决定 tsquery 的**结构**
    （插入 `&`/`!`/括号），那才叫注入；只提供词、由我们决定怎么连接，就不叫。
    """
    query = build_or_tsquery(tokenize(payload))
    if not query:
        return
    for piece in query.split(" | "):
        assert META_CHAR_FREE.match(piece), f"tsquery 里出现了非词片段：{piece!r}"


def test_pure_punctuation_token_is_dropped():
    """
    只由标点组成的 token 对检索毫无价值，还会污染 df 统计 —— 必须丢弃。

    ⚠ 这里测 `_clean_token` 这个私有函数是**有意的**（白盒）：
      它是"不变量"的**唯一**执行点，从公开的 `tokenize` 侧很难构造出
      "清洗后为空"的边界（jieba 的分词行为会掺进来）。测它 = 直接测那条规则。
    """
    assert _clean_token("&|!()<>:*") is None
    assert _clean_token("''''") is None
    assert _clean_token("---") is None
    assert _clean_token("") is None


def test_quote_and_semicolon_never_reach_the_query():
    """
    SQL 的两个关键定界符：`'`（字符串边界）与 `;`（语句分隔符）。
    `'` 由剥除正则直接抹掉；`;` 不在剥除集里，但因为**没有实义字符**被整词丢弃。
    两条路都到不了 tsquery —— 这里把最终结果钉住。
    """
    query = build_or_tsquery(tokenize("'; DROP TABLE messages; --"))
    assert "'" not in query
    assert ";" not in query
    assert query  # 但正常词（DROP/TABLE/messages）该留下，不能一刀切到空


def test_legitimate_query_is_not_over_sanitized():
    """
    ★ 反向断言：清洗**不能**把正常查询搞坏。

    只测"危险的被拦住了"是不够的 —— 一个 `return []` 的实现能通过全部安全断言，
    却让检索彻底失效。所以必须同时有"正常输入要好用"这一侧。
    """
    tokens = tokenize("年假有几天？")
    assert tokens, "正常中文查询被清空了"
    assert any("年假" in t or "年" in t for t in tokens)


# ============================================================
# 二、API 层：恶意路径参数必须是 4xx，不能是 500
# ============================================================


@pytest.mark.db
@pytest.mark.parametrize(
    "payload",
    ["'; DROP TABLE messages; --", "1 OR 1=1", "../../etc/passwd", "%00"],
)
async def test_malicious_case_key_never_causes_500(api_client, payload):
    """
    恶意路径参数的正确结局是 **4xx**，**绝不能是 500**。

    500 意味着这个值以某种方式打到了不该打到的地方；4xx 说明它只是
    一个被拒绝的普通字符串 —— 这正是参数化查询 + 入口校验该有的表现。

    三种 4xx 各有分工：
      · 404 —— 参数合法但查不到（`'; DROP TABLE` 这类：经过参数化后就是一个查不到的字符串）
      · 400 —— 参数本身**非法**（`%00`，NUL 字节；由 `app/main.py` 的中间件拦下）
      · 422 —— 类型/取值不在白名单（由 FastAPI 的类型约束拦下）
    """
    resp = await api_client.get(f"/api/eval/cases/{payload}")
    assert resp.status_code in (400, 404, 422), f"拿到了 {resp.status_code}：{resp.text[:200]}"


@pytest.mark.db
async def test_malicious_query_param_never_causes_500(api_client):
    """查询参数同理（这里打的是类别过滤参数，非法值由 Literal 类型拦成 422）。"""
    resp = await api_client.get("/api/eval/cases", params={"category": "'; DROP TABLE messages; --"})
    assert resp.status_code == 422


# ============================================================
# 待办（不许因为"没对象"就当作已完成）
# ============================================================
# ⏳ D35 语义层落地后必须补写这三类注入用例（PRD §14 原文点名）：
#    ① 维度名注入（dimension）
#    ② 指标名注入（metric）
#    ③ 时间参数注入（time range）
#    判据：非法值必须被**白名单**拦下（拒绝渲染成 SQL），而不是靠转义。
