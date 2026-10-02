"""
MCP stdio 透明中间人（抓包器，D26 产物）
==========================================
它自己**不是** MCP 的任何一方，只是一段双向管道：

    Client 的 stdin  ──> 本进程 ──> 真正 Server 子进程的 stdin
    Client 的 stdout <── 本进程 <── 真正 Server 子进程的 stdout

它把双方之间的**每一行 JSON-RPC 原样抄一份到 stderr**，然后原样转发。
→ 用途：Client 与 Server 明明都跑通了，但你想知道**它们到底说了什么**的时候。

为什么 MCP 调试特别需要它
--------------------------
MCP 的两种传输（stdio）里，协议流走的是 stdin/stdout，**没有网络包可以抓**。
而 SDK 又把报文封在内部，业务代码只能拿到解析后的对象。
所以要回答"它到底发了 `initialize` 还是 `server/discover`"这类问题，
只能在中间插一段管道。**两种说法都能自圆其说，只有报文能分开。**

怎么用
------
别直接跑它，让 MCP Client 去 spawn 它，并把真正的 server 作为参数传进来：

    # Python 侧
    StdioServerParameters(
        command=sys.executable,
        args=["scripts/d26_mitm.py", "/绝对/路径/到/inventory_server.py"],
    )

命令行里 `>` 一下就把报文存下来了（它打的是 stderr）：

    uv run python scripts/d26_mitm.py mcp_servers/inventory_server.py \\
        < request.jsonl > response.jsonl 2> packets.log

两个刻意的设计
--------------
1. **打印不截断**：早期版本用 `raw[:300]` 打印，结果长响应（如整张工具清单）
   在日志里只剩前半截 —— 而"原样落日志"这句表述就失真了。
   这里一律打全文；要截断请在**读日志的人**那边做，别在记录时做。
2. **零依赖**：只用标准库，所以它能在任意 Python 环境里跑，
   不需要跟被测 server 装在同一套依赖里。
"""

import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))


def describe(raw: str) -> str:
    """把一行 JSON-RPC 概括成人话，方便一眼看懂这一行的作用。"""
    try:
        msg = json.loads(raw)
    except Exception:
        return f"(非 JSON) {raw}"

    if "method" in msg and "id" in msg:
        kind = f"请求 method={msg['method']} id={msg['id']}"
    elif "method" in msg:
        kind = f"通知 method={msg['method']}"
    elif "result" in msg:
        kind = f"响应 OK id={msg.get('id')}"
    elif "error" in msg:
        kind = f"响应 报错 id={msg.get('id')} err={msg['error']}"
    else:
        kind = "未知消息"
    return f"{kind}  | 全文: {raw}"


def pump(src, dst, tag: str) -> None:
    """单向搬运：读一行 → 记一行 → 原样转发一行。"""
    try:
        for line in iter(src.readline, ""):
            if not line:
                break
            s = line.strip()
            if s:
                print(f"[报文] {tag}  {describe(s)}", file=sys.stderr, flush=True)
            dst.write(line)
            dst.flush()
    finally:
        try:
            dst.close()
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/d26_mitm.py <要监听的 server 脚本路径>", file=sys.stderr)
        raise SystemExit(2)

    target = sys.argv[1]
    # 绝对路径直接可用；相对路径按本脚本所在目录解析（os.path.join 遇绝对路径会自动重置）
    target_path = target if os.path.isabs(target) else os.path.join(HERE, target)

    if not os.path.exists(target_path):
        print(f"找不到要监听的 server：{target_path}", file=sys.stderr)
        raise SystemExit(2)

    # 子进程用 sys.executable —— 保证它和本脚本跑在同一套 Python 环境里
    # （否则换个解释器就会 import 不到 mcp，报的错还像是"server 坏了"）。
    child = subprocess.Popen(
        [sys.executable, target_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,  # server 自己的 stderr 直接透出去，不拦
        text=True,
        bufsize=1,
    )

    t1 = threading.Thread(target=pump, args=(sys.stdin, child.stdin, "Client → Server"), daemon=True)
    t2 = threading.Thread(target=pump, args=(child.stdout, sys.stdout, "Server → Client"), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    child.wait()


if __name__ == "__main__":
    main()
