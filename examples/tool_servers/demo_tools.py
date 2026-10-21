"""
示例工具：给「上传本地工具」做端到端验收用
============================================
这个文件**不属于**平台代码 —— 它是"用户会传上来的那种东西"的一个真实样本：

· 零第三方依赖（只 import 标准库 + mcp）→ 能在沙箱只读镜像里跑
· 两个工具，一个纯计算、一个用到日期 → 覆盖"有参数/无外部 IO"两种形态
· 绝不往 stdout 打日志 → stdout 是协议专用通道，打了就污染 JSON-RPC

上传方式（两种都行）：
    界面：工具注册表 → 注册工具 → 选这个文件
    命令行：curl -F "file=@examples/tool_servers/demo_tools.py" \
                 -F "name=demo_tools" -F "display_name=示例工具" \
                 http://localhost:18000/api/tool-servers

上传后工具全名会变成 `demo_tools_text_stats` / `demo_tools_days_until`
（`<name>_` 前缀 = 命名空间，防两台 server 的工具撞名）。
"""

import sys
from datetime import date

from mcp.server import MCPServer

server = MCPServer("demo_tools")


@server.tool()
def text_stats(text: str) -> str:
    """统计一段文字的字符数、行数、非空行数与平均每行长度。

    当用户想快速了解一段文本的规模（"这段有多少字""几行"）时使用。
    纯计算，不访问任何外部资源。

    Args:
        text: 要统计的原文（可以含换行）。

    Returns:
        一行可读的统计结果，如「字符 42 / 行 3 / 非空行 2 / 平均每行 14.0」。
    """
    lines = text.splitlines() or [""]
    non_empty = [line for line in lines if line.strip()]
    average = len(text) / len(lines) if lines else 0.0
    return (
        f"字符 {len(text)} / 行 {len(lines)} / "
        f"非空行 {len(non_empty)} / 平均每行 {average:.1f}"
    )


@server.tool()
def days_until(date_str: str) -> str:
    """计算从今天到指定日期还有多少天（已过去的日期返回负数）。

    当用户问"距离某天还有多久""这个截止日期过了吗"时使用。

    Args:
        date_str: 目标日期，格式 YYYY-MM-DD（如 2026-12-31）。

    Returns:
        一句结果，如「距离 2026-12-31 还有 72 天」；格式不对时返回错误说明而不是抛异常。
    """
    try:
        target = date.fromisoformat(date_str.strip())
    except ValueError:
        return f"日期格式不对：{date_str!r}，应为 YYYY-MM-DD"
    delta = (target - date.today()).days
    if delta >= 0:
        return f"距离 {target.isoformat()} 还有 {delta} 天"
    return f"{target.isoformat()} 已过去 {-delta} 天"


if __name__ == "__main__":
    # 只写 stderr：stdout 上跑的必须是 JSON-RPC 报文。
    print("[demo-tools] 已启动，等待 stdin 报文…", file=sys.stderr, flush=True)
    server.run(transport="stdio")
