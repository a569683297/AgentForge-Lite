r"""
分词器（D16）—— 全项目唯一的分词出口
======================================
索引侧（切片入库）与查询侧（用户提问）**都只能调这里的函数**。

为什么必须是单点（D13 §2.5 那条风险，D16 探针 v3 实测过后果）：
    两侧的 token 一旦对不上，检索就**静默失效** —— 不报错、不崩溃，
    只是永远查不到。这类失败最难查，唯一可靠的防法就是「同一个函数」。

清洗分两步，缺一不可（D16 探针 v2/v3 实测依据）：

  ① 丢弃不含实义字符的 token
     jieba 会把标点切成独立 token：'A&B公司' → ['A', '&', 'B', '公司']
     而 tsquery 是把字符串当**表达式**解析的，单独一个 '&' 会直接报语法错。

  ② 剥掉 token 内部残留的 tsquery 元字符
     实测这 9 个字符会让 to_tsquery 抛 PostgresSyntaxError（对用户就是 500）：
         &  |  !  (  )  :  <  >  '  \  *
     '3<5'、'紧急!重要'、'foo(bar)'、'cost: 100' 这类输入在真实场景里很常见 ——
     实测 13 个真实查询串里 6 个会炸（46%），清洗后 0 个。

     ⚠️ 为什么第 ② 步也要做在**两侧共用的函数**里：
        如果只在查询侧剥字符，索引侧存的是未剥的 token，两边又对不上了。
        清洗规则本身也是「分词器的一部分」。

不做的事（有意为之）：
- **不去重**。同一个词出现 3 次就是 tf=3，去重会把 BM25 的词频信号抹掉。
- **不改写大小写**。`simple` 配置在数据库侧本来就会统一小写
  （实测 'Python' 与 'python' 都规范化成 'python'），应用层再动一次是重复劳动。
"""

import re

import jieba

# 静默 jieba 的 logger。不加这行，日志里会混进
# "Building prefix dict ..." / "Loading model cost 0.275 seconds." 之类的噪音。
# 冷启动成本实测 0.275s（一次性，惰性加载词典），量级可忽略，故不做 warmup。
jieba.setLogLevel(60)

# token 之间用空格连接后存库。之所以能这么简单，是因为数据库侧用的是
# `simple` 配置（按空格/标点切）—— 数据库不需要"懂中文"，它只需要输入里已有空格。
TOKEN_SEPARATOR = " "

# 词与词之间要被人为切开的那些 tsquery 元字符（见模块 docstring ①②）
_UNSAFE_CHARS = re.compile(r"[&|!()<>:'\\*]")

# 「含实义字符」= 含数字 / 拉丁字母 / 汉字 / 日文假名。
# 只有标点的 token（'&' '(' '.' 之类）对检索没有任何价值，还会污染 df 统计。
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff]")


def _clean_token(raw: str) -> str | None:
    """
    清洗单个 token；清洗后没有实义字符就返回 None（调用方丢弃）。

    两个动作的顺序有讲究：**先剥元字符、再判有无实义字符**。
    反过来的话，'&' 会先被判为"无实义字符"而丢弃（结果一样），
    但 'a&b' 这种混合 token 会被误判为"有实义字符"而保留下来、带着 & 进 tsquery —— 就炸了。
    """
    token = _UNSAFE_CHARS.sub("", raw)
    if not _HAS_WORD.search(token):
        return None
    return token


def tokenize(text: str) -> list[str]:
    """
    切词主入口：原始文本 → 清洗后的 token 列表。

    索引侧与查询侧都调它，所以两侧的 token 必然一致 —— 这是 D16 全部正确性的地基。

    Args:
        text: 原始文本（切片内容或用户问题）
    Returns:
        token 列表；**可能有重复**（重复次数就是 BM25 的词频 tf，不要去重）
    """
    if not text:
        return []

    tokens: list[str] = []
    for raw in jieba.lcut(text):
        cleaned = _clean_token(raw)
        if cleaned:
            tokens.append(cleaned)
    return tokens


def tokenize_to_string(text: str) -> str:
    """
    切词并拼成空格分隔的字符串 —— 供**索引侧**存进 `document_chunks.content_tokens`。

    入库用字符串而不是数组：生成列 `content_tsv` 由数据库从这一列派生
    （`to_tsvector('simple', content_tokens)`），存成文本最直接。
    """
    return TOKEN_SEPARATOR.join(tokenize(text))


def build_or_tsquery(tokens: list[str]) -> str:
    """
    把 token 序列拼成 **OR** 形式的 tsquery，供候选筛选使用。

    为什么是 OR（D16 决策 1，理由比"召回高"更硬）：
        BM25 中不含任何查询词的文档，每一项的词频都是 0 → 分数恒为 0。
        所以"至少命中一个查询词"这个筛选条件，与"分数可能非 0"是**等价的**
        —— OR 筛出来的候选集不多不少，结果与扫全表完全一致，只是省掉了算零分的成本。
        换成 AND（要求全部命中）则会踢掉"只命中部分词"的文档（它们分数非 0），
        **静默篡改排序结果**。

    为什么这个函数放在 tokenizer 而不是 retrieval_service：
        它成立的前提是「token 已经过 _clean_token 清洗」。和 tokenize() 是同一份契约的
        两端，分到两个文件里，将来有人绕过清洗直接拼 tsquery 就会 500。

    Returns:
        tsquery 字符串；tokens 为空时返回空串。**调用方应判空提前返回**，理由实测如下：
            to_tsquery('simple', '')      → ''      （不报错，是空查询）
            to_tsvector(...) @@ 空查询     → False   （查不到，**不是**"匹配所有"）
        所以空串不会造成错误结果，只是白跑一次数据库往返、语义也不清晰。
        （这一段最初写成"空查询会匹配所有"，是未验证的臆测，实测后已改正。）
    """
    return " | ".join(tokens)
