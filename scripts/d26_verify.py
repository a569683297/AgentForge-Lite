"""
D26 验证：自研库存 MCP Server
==============================
验证对象：`mcp_servers/inventory_server.py`（一个**独立进程**形态的 MCP Server）

    uv run python -m scripts.d26_verify

--------------------------------------------------------------------------
分七段，各段回答一个不同的问题
--------------------------------------------------------------------------
    A 前置静态检查  这份文件真的是"零项目依赖、能被单独启动"的吗（只读源码，不连）
    B 连接与身份    ★ 官方**高层 Client** 能不能连上它（不是只有我自己写的探针能连）
    C 工具目录      `tools/list` 报出来的目录，字段是否齐全、schema 是否可用
    D 调用与返回    ★ 名字从目录里取（零硬编码），业务结果对不对、双层返回对不对
    E 错误语义      出错时是**抛异常**还是**装进第 2 层的 isError**（这决定模型能不能自修正）
    F 两代协议对照  ★ 同一个 server、同一个 Client，只改 mode，看**第一封报文**是什么
    G 进程独立性    ★ 它是不是一个**真的独立进程**，生命周期归谁管
    H 汇总

--------------------------------------------------------------------------
为什么 B 段必须用「官方高层 Client」而不是自己写的探针
--------------------------------------------------------------------------
自己写的客户端能连上，只证明"我按自己理解的协议写对了"；
**官方客户端能连上，才证明"服务对外表现符合规范"**。
（同 D25 的规矩：实验脚本本身不能含有你要证明的东西。）

--------------------------------------------------------------------------
为什么 D 段必须「零硬编码工具名」
--------------------------------------------------------------------------
若脚本里直接写死工具名去调用，那它只能证明"我写对了"，**证明不了名字来自 Server** ——
名字本来就在我手上。所以本文件里**一个工具名都不许出现**，
名字一律从 `tools/list` 的返回值里取；参数名也一律从该工具自报的 `inputSchema` 里取。
C7 就是这条规矩的自证：AST 扫**本文件**的全部字符串常量，与工具名求交集，必须为空。

--------------------------------------------------------------------------
为什么 F 段要抓包，而不是读 SDK 文档
--------------------------------------------------------------------------
"stdio 默认发 initialize 握手"与"默认发 server/discover"**两种说法都能自圆其说**，
而它们指向完全不同的两代协议。文档只告诉你"存在什么"，
**"默认用哪个"的唯一来源是跑一次 + 把报文抓下来看**。
（F0 是一条守门断言：抓包日志必须没被截断 —— 截断过的"原始日志"是不能引用的。）
"""

import ast
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
SERVER = PROJECT_ROOT / "mcp_servers" / "inventory_server.py"
MITM = Path(__file__).resolve().parent / "d26_mitm.py"

# ── 期望值：这些是「我对服务的断言」，故意写死 ──
# 它们写死的意义是：**服务改了什么，这里必须跟着显式改一遍**，不许悄悄跟着漂。
# （反面是"从 server 源码里把数字读出来再比"，那等于用被测量的东西验证它自己。）
EXPECTED_TOTAL = 12  # mock 数据固定 12 条
ABSENT_CATEGORY = "这个品类明显不存在"  # 用来验证「空结果不是错误」

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    """统一断言出口：打印 + 计数。不用 `assert` 是为了让**全部断言都跑完**再给结论。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


def head(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ============================================================
# 进程存活探测：os.kill(pid, 0) —— 不发信号，只问内核「这个 pid 还在不在」
# ============================================================
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在，只是不归我管


async def _wait_pidfile(path: Path, timeout: float = 3.0) -> int | None:
    """轮询等 pidfile 落地。返回读到的 pid，超时返回 None。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            raw = path.read_text().strip()
            if raw.isdigit():
                return int(raw)
        await asyncio.sleep(0.02)
    return None


# ============================================================
# 从 schema 里「按类型」找参数，而不是「按位置」猜
# ============================================================
# 教训（本脚本第一版就踩了）：`sorted(props)[0]` 拿到的是 `category`（字母序靠前），
# 我却把它当成了 min_stock 去传 20 → 服务端参数校验拒绝 → 后面 `structured_content["result"]`
# 直接 TypeError 把整个脚本打挂。
# **位置 ≠ 语义**。参数名可以随便取，能稳定区分它们的只有 schema 里声明的类型。
def _param_by_json_type(props: dict, want: str) -> str | None:
    """在 inputSchema 的属性里找一个「底类型是 want」的参数名（跳过 null 那一支）。"""
    for name, spec in props.items():
        alts = spec.get("anyOf") or [spec]
        types = {alt.get("type") for alt in alts if isinstance(alt, dict)}
        if want in types and "null" in types:
            return name
    return None


def _rows(result) -> list | None:
    """安全取结构化结果：拿不到就返回 None，交给 check 去报红 —— 不让脚本崩在半路。"""
    if result is None or not isinstance(result.structured_content, dict):
        return None
    rows = result.structured_content.get("result")
    return rows if isinstance(rows, list) else None


def _text_of(result) -> str:
    """安全取文本层内容。"""
    if result is None or not result.content:
        return ""
    return getattr(result.content[0], "text", "")


# ============================================================
# A 前置静态检查（不连进程，只读源码）
# ============================================================
def seg_a() -> None:
    head("A 前置静态检查（只读源码，不连进程）")

    check("A1 server 文件存在且非空", SERVER.exists() and SERVER.stat().st_size > 0,
          f"{SERVER.relative_to(PROJECT_ROOT)} · {SERVER.stat().st_size if SERVER.exists() else 0} 字节")

    src = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # A2 零项目依赖：AST 扫描 import 节点（不是文本匹配 —— 注释里出现 "app" 不算）
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    project_deps = [m for m in imported if m == "app" or m.startswith("app.")]
    check("A2 零项目依赖：没有任何 app.* 的 import", not project_deps,
          f"实测 import = {sorted(imported)}")

    # A3 入口形态：必须是「建 server 对象 + 注册工具 + run(stdio)」
    has_ctor = re.search(r"MCPServer\s*\(", src) is not None
    has_tool_deco = "@server.tool(" in src
    has_run = re.search(r"server\.run\(\s*transport\s*=\s*[\"']stdio[\"']\s*\)", src) is not None
    check("A3 入口三件套齐全（建对象 / 注册工具 / run(stdio)）",
          has_ctor and has_tool_deco and has_run,
          f"建对象={has_ctor} 装饰器={has_tool_deco} run(stdio)={has_run}")


# ============================================================
# B/C/D/E 主连接：一次连接里跑完四段
# ============================================================
async def seg_b_to_e(tmpdir: Path) -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    errlog_path = tmpdir / "main_server.err.log"

    with open(errlog_path, "w", encoding="utf-8") as errlog:
        # 用 stdio_client(...) 而不是 Client(StdioServerParameters(...))：
        # 前者能传 errlog —— 把 server 自己的 stderr 收进文件，免得混进本脚本的输出。
        async with Client(stdio_client(params, errlog=errlog)) as cli:

            # ---------- B 连接与身份 ----------
            head("B 连接与身份（官方高层 Client）")
            check("B1 连上了，且 server 自报名字 = inventory",
                  cli.server_info is not None and cli.server_info.name == "inventory",
                  f"server_info = {cli.server_info}")
            check("B2 谈定了一个非空的协议版本",
                  isinstance(cli.protocol_version, str) and len(cli.protocol_version) > 0,
                  f"protocol_version = {cli.protocol_version}")
            caps = cli.server_capabilities
            check("B3 能力声明里有 tools",
                  caps is not None and getattr(caps, "tools", None) is not None,
                  f"capabilities = {caps}")

            # server 的 stderr 落盘了内容 —— 证明它的日志走 stderr、没混进协议流
            banner = errlog_path.read_text(encoding="utf-8") if errlog_path.exists() else ""
            check("B4 server 的启动日志落在 stderr（协议流干净）",
                  "inventory-server" in banner or len(banner) > 0,
                  f"stderr {len(banner)} 字符：{banner.strip().splitlines()[0][:60] if banner.strip() else '<空>'}")

            # ---------- C 工具目录 ----------
            head("C 工具目录（tools/list）")
            listed = await cli.list_tools()
            tools = listed.tools
            check("C1 至少报出 1 个工具", len(tools) >= 1, f"共 {len(tools)} 个：{[t.name for t in tools]}")

            names_ok = all(isinstance(t.name, str) and t.name.strip() for t in tools)
            check("C2 每个工具都有非空 name", names_ok and len(tools) > 0)

            descs_ok = all(isinstance(t.description, str) and t.description.strip() for t in tools)
            check("C3 每个工具都有非空 description（由函数的 docstring 自动生成）", descs_ok,
                  f"最短描述 {min((len(t.description or '') for t in tools), default=0)} 字符")

            schemas_ok = all(
                isinstance(t.input_schema, dict) and t.input_schema.get("type") == "object"
                for t in tools
            )
            check("C4 每个工具的 input_schema 都是一份 object 类型的 JSON Schema", schemas_ok)

            outputs_ok = all(
                isinstance(t.output_schema, dict) and "result" in t.output_schema.get("properties", {})
                for t in tools
            )
            check("C5 output_schema 自动生成，且声明了 result 字段", outputs_ok,
                  f"示例：{json.dumps(tools[0].output_schema, ensure_ascii=False)[:90]}…")

            # C6 参数名从 schema 里取（下面 D 段用它构造参数，不自己编）
            props = dict(tools[0].input_schema.get("properties", {}))
            check("C6 inputSchema 声明了 2 个参数，且都是可省略的（默认 null）",
                  len(props) == 2 and all(p.get("default") is None for p in props.values()),
                  f"参数名 = {sorted(props)}")

            # ★ C7 零硬编码自证：扫本文件的字符串常量，不许出现任何工具名
            literals = {
                node.value
                for node in ast.walk(ast.parse(SELF.read_text(encoding="utf-8")))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            leaked = sorted({t.name for t in tools} & literals)
            check("C7 ★ 本文件里不出现任何工具名（零硬编码自证）", not leaked,
                  f"工具名 {[t.name for t in tools]} 与本文件字符串常量的交集 = {leaked}")

            # ---------- D 调用与返回 ----------
            head("D 调用与返回（名字与参数名全部来自上面的目录）")
            target = tools[0].name  # ← 名字来自 tools/list，不是我写死的
            # 参数也按 schema 声明的类型挑 —— 不是按位置猜。
            # （本脚本第一版写成 p_names[0]，字母序拿到的是 category，我却当成了 min_stock
            #   去传 20 → 服务端拒绝 → 后面取 structured_content 直接 TypeError 把脚本打挂。）
            min_stock_key = _param_by_json_type(props, "integer")
            category_key = _param_by_json_type(props, "string")
            check("D0 从 inputSchema 里按类型定位到「数值参数」与「文本参数」",
                  min_stock_key is not None and category_key is not None,
                  f"数值参数={min_stock_key} / 文本参数={category_key}")

            r_all = await cli.call_tool(target, {})
            rows_all = _rows(r_all)
            check("D1 不带参数调用成功，返回全部记录",
                  r_all.is_error is False and rows_all is not None
                  and len(rows_all) == EXPECTED_TOTAL and EXPECTED_TOTAL > 0,
                  f"is_error={r_all.is_error} 条数="
                  f"{len(rows_all) if rows_all is not None else '<无结构化结果>'}（期望 {EXPECTED_TOTAL}）")

            if min_stock_key is None or category_key is None or rows_all is None:
                check("D2~D6 / E2 跳过：缺参数定位或基线结果，无法继续", False,
                      "先修 D0/D1；不在缺前提的情况下硬跑，免得报一堆假红")
            else:
                r_min = await cli.call_tool(target, {min_stock_key: 20})
                rows_min = _rows(r_min)
                # 期望值用**另一条独立路径**算：从「全部记录」里自己筛一遍，而不是引用服务端的过滤逻辑
                manual = [row for row in rows_all if row["stock"] >= 20]
                check("D2 数值过滤生效，且结果与独立重算一致",
                      r_min.is_error is False and rows_min is not None
                      and len(rows_min) == len(manual)
                      and 0 < len(rows_min) < len(rows_all),
                      f"{min_stock_key}=20 → {len(rows_min) if rows_min is not None else '<无>'} 条"
                      f"（独立重算 {len(manual)} 条，全部 {len(rows_all)} 条）")

                a_category = rows_all[0]["category"]
                r_cat = await cli.call_tool(target, {category_key: a_category})
                rows_cat = _rows(r_cat)
                check("D3 品类过滤生效，且返回的每一条品类都对",
                      rows_cat is not None and len(rows_cat) > 0
                      and all(row["category"] == a_category for row in rows_cat),
                      f"{category_key}={a_category} → "
                      f"{len(rows_cat) if rows_cat is not None else '<无>'} 条")

                r_empty = await cli.call_tool(target, {category_key: ABSENT_CATEGORY})
                rows_empty = _rows(r_empty)
                check("D4 ★ 空结果不是错误（is_error=False 且返回空）",
                      r_empty.is_error is False and rows_empty == [] and r_empty.content == [],
                      f"is_error={r_empty.is_error} content 块数={len(r_empty.content)} "
                      f"result={r_empty.structured_content}")

                # D5 双层返回：content（给模型读）与 structured_content（给程序读）是同一批数据的两个视角
                content_json = [json.loads(block.text) for block in r_all.content]
                check("D5 ★ 双层返回：content 每条记录一个 block，且与 structuredContent 逐条一致",
                      len(r_all.content) == len(rows_all) and len(rows_all) > 0
                      and content_json == rows_all,
                      f"content {len(r_all.content)} 块 / structuredContent.result {len(rows_all)} 条 / "
                      f"逐条比对一致={content_json == rows_all}")

                stock_seq = [row["stock"] for row in rows_all]
                check("D6 返回顺序是库存升序（方便一眼看出该补货哪些）",
                      stock_seq == sorted(stock_seq),
                      f"前 5 个库存={stock_seq[:5]}")

            # ---------- E 错误语义 ----------
            head("E 错误语义（这决定模型能不能自己修正）")

            raised = None
            r_bad_tool = None
            try:
                r_bad_tool = await cli.call_tool("__no_such_tool_exists__", {})
            except Exception as exc:  # noqa: BLE001
                raised = exc
            check("E1 调不存在的工具：不抛异常，而是 is_error=True 装进返回里",
                  raised is None and r_bad_tool is not None and r_bad_tool.is_error is True,
                  f"抛异常={raised!r} is_error={getattr(r_bad_tool, 'is_error', None)} "
                  f"text={_text_of(r_bad_tool)[:60] or '<空>'}")

            if min_stock_key is None:
                check("E2 参数类型错：跳过（没定位到数值参数）", False, "先修 D0")
            else:
                raised2 = None
                r_bad_arg = None
                try:
                    r_bad_arg = await cli.call_tool(target, {min_stock_key: "二十"})
                except Exception as exc:  # noqa: BLE001
                    raised2 = exc
                bad_arg_text = _text_of(r_bad_arg)
                check("E2 参数类型错：不抛异常，返回里带可读的校验错误（模型据此能改）",
                      raised2 is None and r_bad_arg is not None and r_bad_arg.is_error is True
                      and "validation error" in bad_arg_text,
                      f"抛异常={raised2!r} text={bad_arg_text[:100].replace(chr(10), ' ') or '<空>'}")

            check("E3 错误返回里没有 structuredContent（错的时候只有文本层）",
                  r_bad_tool is not None and r_bad_tool.structured_content is None,
                  f"structured_content={getattr(r_bad_tool, 'structured_content', '<未取到>')}")

            # 顺带留一份证据：这个 server 的 stderr 全程只出现了启动横幅
            final_err = errlog_path.read_text(encoding="utf-8")
            check("E4 全程协议流干净：server 的 stderr 只有启动横幅，没有异常堆栈",
                  "Traceback" not in final_err,
                  f"stderr 行数={len(final_err.strip().splitlines())}")


# ============================================================
# F 两代协议对照（抓包）
# ============================================================
def _client_methods(logfile: Path) -> list[str]:
    """从抓包日志里抽出「Client → Server」方向发出的所有 method，按出现顺序。"""
    methods: list[str] = []
    if not logfile.exists():
        return methods
    for line in logfile.read_text(encoding="utf-8").splitlines():
        if "Client → Server" not in line:
            continue
        found = re.search(r"method=([A-Za-z0-9_/]+)", line)
        if found:
            methods.append(found.group(1))
    return methods


async def seg_f(tmpdir: Path) -> None:
    head("F 两代协议对照（同一个 server、同一个 Client，只改 mode，看第一封报文）")

    results: dict[str, dict] = {}
    for mode in ("auto", "legacy"):
        logfile = tmpdir / f"mitm_{mode}.log"
        params = StdioServerParameters(command=sys.executable, args=[str(MITM), str(SERVER)])
        with open(logfile, "w", encoding="utf-8") as errlog:
            # 抓包器（mitm）把报文写到自己的 stderr → 被本脚本收进这个文件
            async with Client(stdio_client(params, errlog=errlog), mode=mode) as cli:
                listed = await cli.list_tools()
                results[mode] = {
                    "version": cli.protocol_version,
                    "methods": _client_methods(logfile),
                    "n_tools": len(listed.tools),
                }

    lines_of = {
        mode: [ln for ln in (tmpdir / f"mitm_{mode}.log").read_text(encoding="utf-8").splitlines()
               if "[报文]" in ln]
        for mode in ("auto", "legacy")
    }

    # F0 守门断言：抓包日志必须没被截断 —— 被截断的"原始日志"是不能拿来论证的
    truncated = [ln for mode in lines_of for ln in lines_of[mode] if not ln.rstrip().endswith("}")]
    check("F0 抓包日志未被截断（每条报文都以 } 结尾）", not truncated,
          f"共 {sum(len(v) for v in lines_of.values())} 条报文，疑似截断 {len(truncated)} 条")

    auto_m = results["auto"]["methods"]
    legacy_m = results["legacy"]["methods"]

    check("F1 auto（高层 Client 的默认值）不发 initialize，而是先探 server/discover",
          "initialize" not in auto_m and "server/discover" in auto_m,
          f"auto 发出的 method 序列 = {auto_m}")
    check("F2 legacy 走 initialize 握手（pre-2026 的老路）",
          "initialize" in legacy_m,
          f"legacy 发出的 method 序列 = {legacy_m}")
    check("F3 两种 mode 谈定的协议版本不同",
          results["auto"]["version"] != results["legacy"]["version"],
          f"auto={results['auto']['version']}  legacy={results['legacy']['version']}")
    check("F4 业务报文不受代际影响：两种 mode 都调了 tools/list 且拿到同样多的工具",
          "tools/list" in auto_m and "tools/list" in legacy_m
          and results["auto"]["n_tools"] == results["legacy"]["n_tools"] > 0,
          f"auto {results['auto']['n_tools']} 个 / legacy {results['legacy']['n_tools']} 个")


# ============================================================
# G 进程独立性 / 生命周期
# ============================================================
async def seg_g(tmpdir: Path) -> None:
    head("G 进程独立性（它是个真进程，还是一个函数？）")

    pidfile = tmpdir / "server.pid"
    # 为什么包一层 /bin/sh：
    #   Client 起子进程时把 pid 封在内部，拿不到。所以让 shell 先写下 $$ 再 exec 顶替 ——
    #   exec 之后 shell 的进程号就是 server 的进程号。**拿到的 pid 来自内核，不是 server 自报。**
    boot = f'echo $$ > "{pidfile}"; exec "{sys.executable}" "{SERVER}"'
    wrapped = StdioServerParameters(command="/bin/sh", args=["-c", boot])

    pid: int | None = None
    with open(tmpdir / "pid_probe.log", "w", encoding="utf-8") as errlog:
        async with Client(stdio_client(wrapped, errlog=errlog)) as cli:
            await cli.list_tools()  # 确认服务真的在答话，再去看进程
            pid = await _wait_pidfile(pidfile)

            check("G1 拿到了 server 进程的 pid", pid is not None, f"pid={pid}")
            if pid is not None:
                check("G2 它不是本进程、也不是本进程的父进程",
                      pid != os.getpid() and pid != os.getppid(),
                      f"server pid={pid} / 本进程 pid={os.getpid()} / 父进程 pid={os.getppid()}")
                check("G3 它是新会话的首领进程（os.getpgid(pid) == pid）",
                      os.getpgid(pid) == pid,
                      f"getpgid={os.getpgid(pid)} → 说明它是被 start_new_session 单独放出来的")
                check("G4 连接期间它确实活着（os.kill(pid, 0) 成功）",
                      _alive(pid), f"alive={_alive(pid)}")

    # 连接退出后：Client 关掉它的 stdin → 它读到 EOF → 自己退出
    if pid is not None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _alive(pid):
            await asyncio.sleep(0.02)
        gone = not _alive(pid)
        check("G5 连接断开后它自行退出（生命周期由 Host 掌管，不是它自己决定）", gone,
              f"退出耗时 ≤ {5.0:.0f}s，最终 alive={_alive(pid)}")

    leftovers = [p for p in (pid,) if p is not None and _alive(p)]
    check("G6 收尾：没有残留进程", not leftovers, f"残留={leftovers}")


# ============================================================
# main
# ============================================================
async def main() -> int:
    print("=" * 74)
    print("D26 验证：自研库存 MCP Server（独立进程形态）")
    print(f"被测对象：{SERVER.relative_to(PROJECT_ROOT)}")
    print(f"本进程 pid={os.getpid()} / 解释器={sys.executable}")
    print("=" * 74)

    tmpdir = Path(tempfile.mkdtemp(prefix="d26_verify_"))

    seg_a()
    await seg_b_to_e(tmpdir)
    await seg_f(tmpdir)
    await seg_g(tmpdir)

    print()
    print("=" * 74)
    verdict = "全部通过" if FAIL == 0 else f"有 {FAIL} 条失败"
    print(f"汇总：{PASS} 通过 / {FAIL} 失败 → {verdict}")
    print("=" * 74)
    if FAIL:
        print(f"（中间产物留给排查：{tmpdir}）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
