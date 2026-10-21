"""
JSON 工具箱：给 Agent 用的结构化数据处理
==========================================
定位：现有工具里没有任何一个能处理「用户贴过来的一段 JSON」。
`query_metric` / `current_time` / `search_documents` / `inventory_*` 都是取数据，
而这里做的是**把已有数据变成能用的形状** —— 校验、取值、比对、转表格。

为什么这几个动作值得做成工具而不是让模型自己算：
  · 取值：一段 5KB 的 JSON 里找 `data.items[3].price`，模型要逐字读，既慢又容易数错；
  · 比对：两份配置哪里不一样，靠"读一遍然后说"必然漏，逐路径列才可靠；
  · 格式化：模型说"这段 JSON 合法"其实只是"看起来像"，真正的词法/语法解析只有 json 模块能给。

沙箱约束下的实现方式（三条硬要求，见 docs/工具上传-功能设计与实现）：
  1. 只 import 标准库 + mcp —— 沙箱里没有第三方包，也没有 pip；
  2. 绝不往 stdout 写日志 —— 那是 JSON-RPC 报文通道，污染即断连；
  3. 所有失败都**返回字符串**而不是抛异常 —— 异常会被 SDK 包成协议错误，
     模型看到的是一串栈，而字符串它能读懂并自行纠正参数。

上传后工具全名会带前缀：`json_kit_json_format` 等。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from mcp.server import MCPServer

server = MCPServer("json_kit")


# ---------------------------------------------------------------- 公共小工具


def _kind(value: Any) -> str:
    """把 Python 类型翻成中文，给模型看的。"""
    if isinstance(value, dict):
        return "对象"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, str):
        return "字符串"
    if value is None:
        return "null"
    return "数字"


def _display_width(text: str) -> int:
    """
    算一段文字在等宽终端里占几列。

    为什么要这个：错误指针要靠空格对齐到出错位置，而中文/全角字符
    占 2 列。直接用 `len()` 算，指针会向左偏 —— 看起来"指错了"，
    比不画指针更误导人。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _explain(exc: json.JSONDecodeError, text: str) -> str:
    """把 json 模块的异常翻译成「第几行第几列 + 原文 + 指针」。"""
    head = f"JSON 语法错误：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}"
    lines = text.splitlines()
    if 1 <= exc.lineno <= len(lines):
        line = lines[exc.lineno - 1]
        # 只截出错位置附近的一段，避免超长单行把回复撑爆
        start = max(0, exc.colno - 40)
        snippet = line[start : exc.colno + 40]
        pointer = " " * _display_width(line[start : exc.colno - 1]) + "^"
        head += f"\n{snippet}\n{pointer}"
    return head


def _load(text: str) -> tuple[Any | None, str | None]:
    """解析 JSON。失败返回 (None, 可读原因)，**不抛异常**。"""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, _explain(exc, text)
    except (TypeError, ValueError) as exc:  # 极少见，兜底
        return None, f"无法解析：{exc}"


def _short(value: Any, limit: int = 80) -> str:
    """把任意值压成一行短文本，用于差异输出里展示「旧 → 新」。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return raw if len(raw) <= limit else raw[:limit] + "…"


def _tokens(path: str) -> list[str | int]:
    """
    把 `data.items[0].name` 拆成 `['data', 'items', 0, 'name']`。

    支持：点号分层的键、方括号下标、可选的 `$` 根标记。
    非法字符会被跳过 —— 与其报"路径语法错误"，不如尽量取到值，
    路径写错时下面的"可用键"提示通常已经足够定位。
    """
    cleaned = path.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    tokens: list[str | int] = []
    for match in re.finditer(r"([^.\[\]]+)|\[(\d+)\]", cleaned):
        name, index = match.group(1), match.group(2)
        tokens.append(int(index) if index is not None else name)
    return tokens


# ---------------------------------------------------------------- 工具 1


@server.tool()
def json_format(text: str, indent: int = 2, compact: bool = False) -> str:
    """校验并格式化一段 JSON。

    当用户贴了一段 JSON 想确认它是否合法、想看排版后的样子，
    或者想压成一行塞进请求体 / 日志时使用。

    Args:
        text: 待处理的 JSON 原文。
        indent: 美化时的缩进空格数（2 或 4 最常用）；compact=True 时忽略。
        compact: 为 True 时输出无空白的一行。

    Returns:
        格式化后的 JSON；非法时返回「JSON 语法错误：第 X 行第 Y 列 —— ...」并附出错的原文片段。
    """
    data, error = _load(text)
    if error:
        return error
    if compact:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, ensure_ascii=False, indent=max(0, min(indent, 8)))


# ---------------------------------------------------------------- 工具 2


@server.tool()
def json_query(text: str, path: str = "") -> str:
    """按路径取出 JSON 里的某一个值。

    当用户已经有一大段 JSON（接口返回、日志、配置），只想看其中一项，
    或者想确认某条路径存不存在时使用。比把整段贴给模型再找更省 token、更准。

    Args:
        text: JSON 原文。
        path: 路径，点号分层、数组用下标，如 `data.items[0].name`；
              可省略开头的 `$`；留空则返回整个文档。

    Returns:
        取到的值（JSON 文本）与它的类型；路径不存在时返回该层能用的键或数组长度。
    """
    data, error = _load(text)
    if error:
        return error

    current: Any = data
    walked = "$"
    for token in _tokens(path):
        if isinstance(token, int):
            if not isinstance(current, list):
                return f"路径 {walked} 是{_kind(current)}，不能用下标 [{token}] 取。"
            if not -len(current) <= token < len(current):
                return f"路径 {walked} 只有 {len(current)} 个元素，下标 [{token}] 越界。"
            current = current[token]
            walked += f"[{token}]"
        else:
            if not isinstance(current, dict):
                return f"路径 {walked} 是{_kind(current)}，没有键 {token!r}。"
            if token not in current:
                keys = "、".join(str(key) for key in list(current)[:12]) or "（空对象）"
                return f"路径 {walked} 没有键 {token!r}。可用键：{keys}"
            current = current[token]
            walked += f".{token}"

    return f"{walked} = {json.dumps(current, ensure_ascii=False)}\n类型：{_kind(current)}"


# ---------------------------------------------------------------- 工具 3


def _diff(left: Any, right: Any, path: str, out: list[str]) -> None:
    """递归收集差异。用 `-`/`+`/`~` 三种符号表示「只在左」「只在右」「值变了」。"""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() - right.keys(), key=str):
            out.append(f"- {path}.{key}：只在左边")
        for key in sorted(right.keys() - left.keys(), key=str):
            out.append(f"+ {path}.{key}：只在右边")
        for key in sorted(left.keys() & right.keys(), key=str):
            _diff(left[key], right[key], f"{path}.{key}", out)
        return
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            out.append(f"~ {path}：长度 {len(left)} → {len(right)}")
        for index, (a, b) in enumerate(zip(left, right)):
            _diff(a, b, f"{path}[{index}]", out)
        # 多出来的元素单独点名，避免被 zip 静默吞掉
        for index in range(min(len(left), len(right)), max(len(left), len(right))):
            side = "左" if index < len(left) else "右"
            out.append(f"+ {path}[{index}]：只在{side}边")
        return
    if left != right:
        out.append(f"~ {path}：{_short(left)} → {_short(right)}")


@server.tool()
def json_compare(left: str, right: str) -> str:
    """比较两段 JSON 的差异，逐路径列出少、多、改了哪些字段。

    当用户想对比两份接口返回、两版配置、改动前后有什么不同时使用。
    用文字描述差异必然漏项，逐路径列出才可靠。

    Args:
        left: 作为基准的那份 JSON（旧）。
        right: 用来比较的那份 JSON（新）。

    Returns:
        差异清单，`-` 表示只在左边、`+` 表示只在右边、`~` 表示值变了；
        完全相同时返回「两份 JSON 结构一致」。
    """
    left_data, left_error = _load(left)
    if left_error:
        return "左侧" + left_error
    right_data, right_error = _load(right)
    if right_error:
        return "右侧" + right_error

    out: list[str] = []
    _diff(left_data, right_data, "$", out)
    if not out:
        return "两份 JSON 结构一致（键、顺序无关的取值完全相同）。"
    total = len(out)
    if total > 100:
        out = out[:100] + [f"…… 差异过多，仅显示前 100 条（实际共 {total} 条）"]
    return f"共 {total} 处差异：\n" + "\n".join(out)


# ---------------------------------------------------------------- 工具 4


@server.tool()
def json_to_table(text: str, columns: str = "") -> str:
    """把一个「对象数组」转成 Markdown 表格。

    当用户拿到一组结构化记录（接口返回的列表、查询结果、日志条目），
    想直接以表格形式查看或贴进文档时使用。

    Args:
        text: JSON 原文，顶层必须是 `[{...}, {...}]` 这样的对象数组。
        columns: 可选的列名，逗号分隔，按给定顺序输出；留空则自动收集所有出现过的键。

    Returns:
        Markdown 表格文本；首行是表头，超过 50 行只显示前 50 行并注明总数。
    """
    data, error = _load(text)
    if error:
        return error
    if not isinstance(data, list) or not data:
        return '需要一个非空的对象数组，例如 [{"a": 1}, {"a": 2}]。'
    if not all(isinstance(row, dict) for row in data):
        return "数组里每个元素都必须是对象（不能是数字、字符串或嵌套数组）。"

    if columns.strip():
        headers = [name.strip() for name in columns.split(",") if name.strip()]
    else:
        headers = []
        for row in data:
            for key in row:
                if key not in headers:
                    headers.append(key)
    if not headers:
        return "所有对象都是空的，没有可展示的列。"

    def cell(value: Any) -> str:
        # 竖线会破坏表格结构，换行会破坏行结构 —— 都必须替换掉
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return raw.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in data[:50]:
        lines.append("| " + " | ".join(cell(row.get(name, "")) for name in headers) + " |")

    if len(data) > 50:
        lines.append(f"\n（共 {len(data)} 行，仅显示前 50 行）")
    return "\n".join(lines)


if __name__ == "__main__":
    # 只写 stderr：stdout 上跑的必须是 JSON-RPC 报文。
    import sys

    print("[json-kit] 已启动，等待 stdin 报文…", file=sys.stderr, flush=True)
    server.run(transport="stdio")
