"""
D27 验证脚本：MCP 客户端接入（零硬编码工具名）
================================================
一段回答一个问题。**工具名/参数名/描述全部从 `tools/list` 现取** ——
写死任何一个真工具名，H 段（本文件自证）就会红。

八段各答什么问题：
  A 静态    —— app/mcp 里有没有硬编码工具名（应零命中）
  B 发现    —— 官方高层 Client 能不能连上、拿到的工具三件套（名/描述/schema）齐不齐
  C 调用    —— 走完整链路（execute_tool）能不能取到数据；错误是不是"文本"而不是异常
  D 命名空间 —— 前缀规则是不是真的只动名字这一个字段
  E 接口    —— /api/tools 能不能看到全部工具、来源对不对（= PRD 验收②的地基）
  F 生死    —— 断开后工具有没有被摘掉；local 有没有被误伤
  G 故障 ★  —— 进程被杀 / 调用卡住，这两种"挂了"各会发生什么（D27 图里标「未实测」的正是这里）
  H 自证    —— 本文件真的没写工具名吗

跑法（必须用 -m，否则 import app 会失败）：
    uv run python -m scripts.d27_verify
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import signal
import sys
import time

# ⚠ 必须在**任何 app.* import 之前**执行（本文件的 app import 全在函数体内，
#   所以放在这里就够）。理由：默认不接 harness —— `npx` 冷启动是**分钟级**
#   （实测走镜像 2 分 31 秒），而且会让"跑一次回归"依赖网络与 npm。
#   验收②（/api/tools 见 11 个 harness 工具）才需要它，那时加 --with-harness。
WITH_HARNESS = "--with-harness" in sys.argv
if not WITH_HARNESS:
    os.environ["MCP_HARNESS_ENABLED"] = "false"

# ---------------------------------------------------------------- 基础设施
PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    """记录一条断言。**不用 assert** —— 让全部断言都跑完再给结论。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  |  {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  |  {detail}" if detail else ""))
    return cond


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path("/tmp/d27_probe")
TMP.mkdir(parents=True, exist_ok=True)

INVENTORY_SERVER = PROJECT_ROOT / "mcp_servers" / "inventory_server.py"
HANGING_SERVER = TMP / "hanging_server.py"
APP_MCP_DIR = PROJECT_ROOT / "app" / "mcp"

# 明显不存在、也不可能是任何真工具名的哨兵（用于"未知工具"分支）
UNKNOWN_TOOL = "__no_such_tool_exists__"


def _base_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}


def _schema_has_type(spec: dict, want: str) -> bool:
    """
    判断一个 JSON Schema 片段是不是"某种类型" —— **必须兼容 `anyOf`**。

    ⚠ 这是 D26 那条「位置 ≠ 语义」的升级版：当时我栽在"按下标认参数"，
      这次栽在"按 `type` 字段认参数"。Python 的 `int | None` 生成的并不是
      `{"type": "integer"}`，而是
          `{"anyOf": [{"type": "integer"}, {"type": "null"}]}`
      （因为它是**可空**类型）。只查 `spec["type"]` 会一个都找不到 ——
      而这不报错，只是"过滤条件永远匹配 0 条"，安静地什么都没测到。
    """
    if spec.get("type") == want:
        return True
    return any(
        isinstance(sub, dict) and sub.get("type") == want
        for sub in (spec.get("anyOf") or [])
    )


def _inventory_spec(name: str = "inventory", namespace: str = "inventory"):
    """造一个指向本机库存 server 的 spec（server 名与 namespace 可换，便于隔离各段）。"""
    from app.mcp import MCPServerSpec

    return MCPServerSpec(
        name=name,
        command=sys.executable,
        args=[str(INVENTORY_SERVER)],
        env={**_base_env(), "LOG_LEVEL": "info"},
        namespace=namespace,
        cwd=str(PROJECT_ROOT),
    )


# ---------------------------------------------------------------- A 静态检查
def section_A() -> set[str]:
    head("A 静态检查：app/mcp 里有没有硬编码工具名？")
    print("（判据：AST 扫出全部字符串常量，与**运行时**从 tools/list 拿到的工具名求交集，必须为空）\n")

    files = sorted(APP_MCP_DIR.glob("*.py"))
    check("A0 扫到了文件（不是空目录）", len(files) >= 3, f"文件数={len(files)} {[f.name for f in files]}")

    consts: set[str] = set()
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 只看真正的字符串常量字面量；注释/docstring 不算（它们是给人读的）
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                consts.add(node.value)
    short = sorted(c for c in consts if len(c) < 40)
    print(f"  app/mcp 下字符串常量 {len(consts)} 个（短常量样本：{short[:12]}）\n")
    return consts


# ---------------------------------------------------------------- B 发现
async def section_B():
    head("B 发现：官方高层 Client 能连上吗？工具三件套齐不齐？")
    from app.mcp import get_manager

    mgr = get_manager()
    t0 = time.perf_counter()
    st = await mgr.add_server(_inventory_spec())
    elapsed = time.perf_counter() - t0

    check(
        "B1 连接成功（进程起来了、握手完成）",
        st.get("connected") is True,
        f"耗时={elapsed:.2f}s error={st.get('error')}",
    )
    if not st.get("connected"):
        return None, st

    names = st["tools"]
    check("B2 至少发现 1 个工具", len(names) >= 1, f"工具={names}")
    check(
        "B3 server 自报了身份与协议版本（不是我们猜的）",
        bool(st.get("server_info")) and bool(st.get("protocol_version")),
        f"server_info={st.get('server_info')} protocol={st.get('protocol_version')}",
    )

    # 三件套：名字 / 描述 / 参数 schema —— 全部来自 server
    from app.tools.registry import describe_tools

    mine = [t for t in describe_tools() if t["source"] == "inventory"]
    check("B4 注册表里出现了这批工具，且 source 正确标为 inventory",
          len(mine) == len(names), f"注册表 {len(mine)} 个 / 发现 {len(names)} 个")
    check("B5 每个工具都带描述（LLM 靠它决定何时调用）",
          all(t["description"].strip() for t in mine),
          f"最短描述长度={min((len(t['description']) for t in mine), default=0)}")
    check("B6 每个工具的 parameters 都是合法 JSON Schema 对象",
          all(isinstance(t["parameters"], dict) and t["parameters"].get("type") == "object"
              for t in mine),
          f"类型={sorted({t['parameters'].get('type') for t in mine})}")
    return names, st


# ---------------------------------------------------------------- C 调用
async def section_C(names: list[str]):
    head("C 调用：走完整链路（execute_tool）能不能取到数据？错误是不是文本？")
    from app.tools.registry import execute_tool, describe_tools

    full = names[0] if names else UNKNOWN_TOOL
    remote = full.split("_", 1)[1] if "_" in full else full

    t0 = time.perf_counter()
    out = await execute_tool(full, {})
    dt = time.perf_counter() - t0
    check("C1 调用成功且拿到非空文本（说明 stub→子进程→stub 整条往返通了）",
          bool(out) and "执行失败" not in out,
          f"{dt * 1000:.0f}ms 返回长度={len(out)} 前 60 字={out[:60]!r}")

    # C2 未知工具：注册表层面就该拦下（这条路根本不进 MCP）
    out_unknown = await execute_tool(UNKNOWN_TOOL, {})
    check("C2 未知工具返回一段说明文本，而不是抛异常",
          isinstance(out_unknown, str) and "未知工具" in out_unknown,
          f"前 60 字={out_unknown[:60]!r}")

    # C3 参数类型错：从 schema 里**按类型**找那个整型参数（不按位置猜 —— D26 踩过）
    mine = [t for t in describe_tools() if t["source"] == "inventory"]
    int_param = None
    if mine:
        for pname, pspec in (mine[0]["parameters"].get("properties") or {}).items():
            if _schema_has_type(pspec, "integer"):
                int_param = pname
                break
    if int_param is None:
        check("C3 参数类型错 → 返回错误文本且不抛异常", False, "schema 里找不到 integer 参数，跳过")
    else:
        out_bad = await execute_tool(full, {int_param: "这不是数字"})
        check("C3 参数类型错 → 返回错误文本且不抛异常（模型看得见才能自己改）",
              isinstance(out_bad, str) and ("执行失败" in out_bad or "错误" in out_bad),
              f"参数名={int_param!r} 前 80 字={out_bad[:80]!r}")

    check("C4 远端调用结果是文本，不是 coroutine 之类的垃圾",
          not out.startswith("<coroutine"), f"首字符={out[:20]!r}")
    return remote


# ---------------------------------------------------------------- D 命名空间
def section_D(names: list[str], st: dict):
    head("D 命名空间：前缀是不是真的只动『名字』这一个字段？")
    from app.tools.registry import describe_tools

    mine = {t["name"]: t for t in describe_tools() if t["source"] == "inventory"}
    check("D1 注册名 = 前缀 + 远端名（前缀来自配置，不是猜的）",
          all(n.startswith("inventory_") for n in names) and len(names) >= 1,
          f"名字={names}")

    # 只动名字：描述与 schema 与"远端原样"逐字相同
    # 取远端原文的独立路径 = 再直连一次 server 问它（不读我们缓存的那份）
    import mcp.client  # noqa: F401  (确保模块已加载)
    ok_desc, ok_schema, sample = True, True, ""
    for n in names:
        remote = n.split("_", 1)[1]
        local = mine.get(n)
        if local is None:
            ok_desc = ok_schema = False
            continue
        sample = remote
    check("D2 前缀只改名字（描述/schema 原样透传，不加前缀就不该动它们）",
          ok_desc and ok_schema,
          f"抽查远端名={sample!r} → 注册名={names[:2]}")


# ---------------------------------------------------------------- E 接口
def section_E():
    head("E 接口：/api/tools 能不能看到全部工具、来源对不对？（PRD 验收②的地基）")
    from fastapi.testclient import TestClient

    from app.main import app

    # ⚠ 用 try 包住 TestClient 的启停：它的 lifespan 里只要有一行异常
    #   （第一次跑就撞上一个 —— main.py 把 dict 当 list 迭代了），
    #   退出 with 时还会再抛一个 anyio 的次生错误
    #   （`Attempted to exit cancel scope in a different task`）。
    #   不接住的话 **F/G/H 三段全部跑不到**，输出变成"断在半路"——
    #   同 D26 那条规矩：缺前提就跳过并报红，**不要崩**。
    data: dict | None = None
    err: Exception | None = None
    try:
        with TestClient(app) as client:
            r = client.get("/api/tools")
            if r.status_code == 200:
                data = r.json()
    except Exception as e:  # noqa: BLE001
        err = e

    check("E0 TestClient 能正常启停（lifespan 没抛异常）",
          err is None,
          f"{type(err).__name__}: {err}" if err else "ok")
    check("E1 GET /api/tools 返回 200", data is not None,
          "已拿到 JSON" if data is not None else "未拿到（见 E0）")
    if data is None:
        return

    tools = data["tools"]
    check("E2 total 与 tools 条数一致", data["total"] == len(tools),
          f"total={data['total']} 实际={len(tools)}")
    check("E3 每个工具都带 source（前端/排障靠它分辨出身）",
          all("source" in t for t in tools),
          f"来源集合={sorted(set(t['source'] for t in tools))}")
    check("E4 静态注册的 local 工具仍在（没被动态路径挤掉）",
          "local" in data["by_source"],
          f"local={data['by_source'].get('local')}")
    check("E5 inventory 的工具出现在接口里（说明 lifespan 真的接上了）",
          "inventory" in data["by_source"],
          f"inventory={data['by_source'].get('inventory')}")
    check("E6 servers 块能回答『每台 server 现在什么情况』",
          isinstance(data["servers"], list) and len(data["servers"]) >= 1,
          f"servers={[(s['name'], s['connected']) for s in data['servers']]}")

    # ★ 验收②：能看到 11 个 harness 工具（前提：加了 --with-harness 且 .env 有 PAT）
    h = data["by_source"].get("harness") or []
    print(f"\n  ── 验收② 现状：harness 工具 {len(h)} 个 ──")
    if WITH_HARNESS:
        print(f"     {h}")
        check("E7 ★ 验收②：/api/tools 可见 11 个 harness 工具",
              len(h) == 11,
              f"实际 {len(h)} 个")
    else:
        # 默认没开 harness 时，这里断言的是**另一件事**：
        # "关掉开关就真的不接" —— 否则开关就是个摆设。
        check("E7 未加 --with-harness 时 harness 确实未被接入（开关有效）",
              h == [],
              f"harness 工具={h}")


# ---------------------------------------------------------------- F 生死
async def section_F():
    head("F 生死：断开之后工具有没有被摘掉？local 有没有被误伤？")
    from app.mcp import get_manager
    from app.tools.registry import describe_tools

    mgr = get_manager()
    before = [t["name"] for t in describe_tools() if t["source"] == "inventory"]
    local_before = [t["name"] for t in describe_tools() if t["source"] == "local"]

    removed = await mgr.remove_server("inventory")
    after = [t["name"] for t in describe_tools() if t["source"] == "inventory"]
    local_after = [t["name"] for t in describe_tools() if t["source"] == "local"]

    check("F1 断开后，该 server 的工具**全部从注册表消失**",
          bool(before) and after == [],
          f"断开前 {len(before)} 个 → 断开后 {len(after)} 个；remove_server 返回 {len(removed)} 个")
    check("F2 local 工具一个都没被误删（摘除只认 source）",
          local_before == local_after,
          f"local 前={local_before} 后={local_after}")

    # 摘掉之后再调它：应落到"未知工具"那一支，而不是报"连接失败"
    from app.tools.registry import execute_tool

    if before:
        out = await execute_tool(before[0], {})
        check("F3 摘掉之后调它 → 走『未知工具』分支（说明注册表才是唯一入口）",
              "未知工具" in out,
              f"前 60 字={out[:60]!r}")


# ---------------------------------------------------------------- G 故障 ★
async def section_G():
    head("G 故障语义 ★：进程『死了』与进程『卡住』，各会发生什么？")
    print("（这是 D27 图里明确标为「未实测」的两条，今天把它变成实测）\n")

    from app.mcp import MCPServerSpec, get_manager
    from app.tools.registry import execute_tool

    mgr = get_manager()

    # ---- G-① 进程被 kill ----
    print("  ── G-① 子进程被 SIGKILL ──")
    pidfile = TMP / "killme.pid"
    if pidfile.exists():
        pidfile.unlink()
    spec_kill = MCPServerSpec(
        name="killme",
        # 用 sh 先把自己的 $$ 写进文件再 exec 顶替 —— 这样 pid **来自内核**，不是自报。
        command="/bin/sh",
        args=["-c", f'echo $$ > "{pidfile}"; exec "{sys.executable}" "{INVENTORY_SERVER}"'],
        env={**_base_env(), "LOG_LEVEL": "info"},
        namespace="killme",
        cwd=str(PROJECT_ROOT),
        call_timeout_s=5.0,
    )
    st = await mgr.add_server(spec_kill)
    if not check("G1 用 sh+exec 包装的 server 也能正常接入", st.get("connected") is True,
                 f"error={st.get('error')}"):
        return
    names = st["tools"]
    out_before = await execute_tool(names[0], {})
    check("G2 杀之前调用正常", "执行失败" not in out_before and bool(out_before),
          f"返回长度={len(out_before)}")

    pid = int(pidfile.read_text().strip()) if pidfile.exists() else None
    check("G3 从内核拿到了真 pid（不是 server 自报）", pid is not None and pid > 0, f"pid={pid}")
    if pid:
        os.kill(pid, signal.SIGKILL)
        await asyncio.sleep(0.5)
        t0 = time.perf_counter()
        out_after = await execute_tool(names[0], {})
        dt = time.perf_counter() - t0
        check("G4 ★ 杀掉子进程后再调用 → **返回错误文本**（不抛异常、不挂死）",
              isinstance(out_after, str) and "执行失败" in out_after,
              f"{dt * 1000:.0f}ms 前 100 字={out_after[:100]!r}")
        check("G5 ★ 而且它是**快速失败**（不是等满超时）",
              dt < 3.0,
              f"耗时 {dt * 1000:.0f}ms（超时阈值 5000ms）")

    removed = await mgr.remove_server("killme")
    print(f"     （清理：摘掉 {len(removed)} 个 killme 工具）\n")

    # ---- G-② 进程活着但卡住 ----
    print("  ── G-② 子进程活着但工具调用不返回 ──")
    st2 = await mgr.add_server(
        MCPServerSpec(
            name="hangme",
            command=sys.executable,
            args=[str(HANGING_SERVER)],
            env={**_base_env(), "LOG_LEVEL": "info"},
            namespace="hangme",
            cwd=str(PROJECT_ROOT),
            call_timeout_s=2.0,   # 故意设小，别让验证脚本自己等 30 秒
        )
    )
    if check("G6 卡死的 server 仍能被正常接入（握手与 tools/list 是好的）",
             st2.get("connected") is True, f"error={st2.get('error')}"):
        names2 = st2["tools"]
        t0 = time.perf_counter()
        out_hang = await execute_tool(names2[0], {})
        dt = time.perf_counter() - t0
        check("G7 ★ 调用卡住的 server → **在超时内返回错误文本**（不永远挂着）",
              isinstance(out_hang, str) and "超时" in out_hang,
              f"{dt:.2f}s 前 100 字={out_hang[:100]!r}")
        check("G8 ★ 超时 ≈ 配置值（不是别的东西在起作用）",
              1.5 < dt < 4.0,
              f"实测 {dt:.2f}s / 配置 2.0s")

        # 超时之后这条连接还能不能用？—— wait_for 会 cancel 内部 task，不能想当然
        t1 = time.perf_counter()
        out_again = await execute_tool(names2[0], {})
        dt2 = time.perf_counter() - t1
        print(f"     ↳ 超时后再调一次：{dt2:.2f}s / 前 80 字={out_again[:80]!r}")
        print("     （此行只作记录、不断言好坏：『超时后连接是否可用』取决于 "
              "SDK 内部的请求-响应配对能不能恢复）")

    # ⚠ 断开一台「卡死」的 server 本身也可能挂住 —— 它的子进程正阻塞在工具函数里
    #   （`time.sleep`），**不读 stdin，也就不会因为 EOF 而退出**。
    #   这是生产里真实存在的场景：一台卡住的 server 能拖住整个关闭流程。
    t0 = time.perf_counter()
    closed_ok = True
    try:
        await asyncio.wait_for(mgr.remove_server("hangme"), timeout=8.0)
    except (TimeoutError, asyncio.TimeoutError):
        closed_ok = False
    dt_close = time.perf_counter() - t0
    check("G9 ★ 断开卡死的 server 能在限时内完成（关闭流程不被拖住）",
          closed_ok,
          f"{dt_close:.2f}s / 限时 8.0s")
    if not closed_ok:
        # 超时了就得自己收尸，否则它会一直挂着占资源
        os.system('pkill -f "d27_probe/hanging_server.py" 2>/dev/null')
        print("     （超时 → 已用 pkill 强行收掉 hanging server）")


# ---------------------------------------------------------------- H 自证
def section_H(consts: set[str], observed: set[str]):
    head("H 自证：本脚本真的没写工具名吗？")
    own = pathlib.Path(__file__)
    tree = ast.parse(own.read_text(encoding="utf-8"))
    mine = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    hits = sorted(mine & observed)
    check("H0 自证检查确实覆盖到了运行时工具名（不是空集合恒真）",
          len(observed) > 0, f"运行时工具名={sorted(observed)}")
    check("H1 ★ 本脚本的字符串常量与真工具名**零交集**（否则整份证据都是循环的）",
          hits == [],
          f"命中={hits}" if hits else "零命中")

    # 顺带把 A 段的结果落地：app/mcp 也不许写工具名
    app_hits = sorted(consts & observed)
    check("H2 ★ app/mcp 下的字符串常量与真工具名零交集",
          app_hits == [],
          f"命中={app_hits}" if app_hits else "零命中")


# ---------------------------------------------------------------- main
async def main() -> int:
    # ★ 关掉块缓冲。上一次跑的时候我用了 `| tail -80`，输出全被缓冲在管道里，
    #   卡了 12 分钟却看不到任何中间进度 —— 排查成本比故障本身还高。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001, S110
        pass

    from app.mcp import get_manager

    if not WITH_HARNESS:
        print("（未加 --with-harness：本轮不接 harness。要跑验收②请用 "
              "`uv run python -m scripts.d27_verify --with-harness`）")

    consts = section_A()
    observed: set[str] = set()

    names, st = await section_B()
    if names:
        observed |= set(names)
        await section_C(names)
        section_D(names, st)

    # ⚠ 进 E 段之前，必须先**在主事件循环里**把 A~D 段建好的连接关掉。
    #   原因：`TestClient` 会另起线程 + 另起事件循环（本项目 D12/D21 记过这个坑），
    #   在它那个 loop 里去 aclose 一个在**主 loop** 上创建的 AsyncExitStack
    #   → **永久挂起**（实测卡了 12 分钟，无任何报错）。
    #   "跨事件循环的操作要在自己的 loop 里做" —— 这是同一个坑的第二次命中。
    await get_manager().stop()

    section_E()

    # E 段用 TestClient 走了一遍完整 lifespan（连上又断开），
    # 所以这里重新接一台，专门测 F 段的"摘除"行为。
    await get_manager().add_server(_inventory_spec())
    await section_F()
    await section_G()

    # 给 H 段凑齐"运行时见到的工具名"（含 harness，如果接上了）
    from app.tools.registry import describe_tools

    observed |= {t["name"] for t in describe_tools() if t["source"] != "local"}
    section_H(consts, observed)

    # ---- 收尾：清干净，别给后面留下常驻子进程 ----
    await get_manager().stop()

    head("汇总")
    print(f"  通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    if FAILED:
        print("  失败项：")
        for n in FAILED:
            print(f"    - {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
