"""
MCP 预热脚本（warmup_mcp.py）—— **演示前必跑**
================================================
PRD §17.3 演示前置检查清单第 2 项；PRD §9.8「提供 make warmup」。

## 为什么需要它

`harness` 那台 server 由 `npx` 拉起，而它第一次启动要现做一堆准备工作。
这些成本**分四层，成因各不相同** —— 只有前两层能靠"提前跑"消掉：

| 层 | 慢在哪 | 一次性的？ |
|---|---|---|
| ① npm 包下载 | 首次要从 registry 下 `harness-mcp-v2`（实测直连 npmjs 超时，走镜像 2 分 31 秒） | ✅ 一次性，之后落 `~/.npm/_npx/` |
| ② 版本的"再解析" | 配置写 `@latest`，那是个**会动的指针** → npx **每次启动都要联网问 registry「latest 是哪个版本」** | ❌ **每次都发生** |
| ③ Node 进程 + 模块加载 | 起 node 进程、把一个几百 MB 的包加载进内存 | ❌ 每次都发生 |
| ④ ONNX 模型 | harness 的语义检索要下约 23 MB 模型 | ✅ 一次性 |

## 为什么不在 API 启动期做

`app/main.py` 的 lifespan 已经会接 MCP，且失败会降级 —— 但降级只解决
"**起不来也不崩**"，**不解决"第一问卡住"**：API 起来了，第一个用户提问要现拉包，
用户盯着转圈几分钟。而且 `npx` 冷启动是分钟级，**串进 compose 启动链会把
`docker compose up` 一起卡住**（D34 验收是"一键起"）。
→ 所以：**启动期不预热，单独一个脚本、演示前手动跑。**

⚠ 本项目 api 容器**不装 Node**（D34 决策）→ 容器内起不了 harness →
本脚本**只在宿主机跑**。这是对 PRD §9.8「串入 compose 启动脚本」的**已知偏差**，
走的是它后半句「或提供 `make warmup`」。

## 它做什么（8 段）

  A 缓存   —— 本地 npm 缓存里有没有 harness-mcp-v2、哪一版、多大（**不起进程**）
  B 配置   —— 当前生效的命令/参数/镜像/只读开关（**PAT 只报有没有，绝不打印**）
  C 起进程 —— T1 = 起 node 子进程 + 完成握手（`connect()` 返回）
  D 发现   —— T2 = `tools/list` 耗时 + 工具清点（应为 11 个）
  E 调用 ★ —— T3 = 真调一次**只读**工具（补 2026-10-06 到期日之后挂起的那笔死账）
  F 回收   —— 断开后确认**没留下孤儿 node 进程**（PRD 风险表点名的「Node 子进程泄漏」）
  G 库存   —— 顺带体检本机自研 inventory server（毫秒级，**不预热**，只确认它在）
  H 报告   —— 汇总 + 退出码

## 退出码（演示前检查清单需要它可判定）

  0 = 预热成功（缓存就绪 + 连接成功 + 工具已列 + 一次调用成功 + 无孤儿）
  1 = 预热失败（连不上 / 调不通 → 演示会翻车，必须先修）
  2 = 前置不满足（没配 PAT，或 harness 开关是关的 → **不是坏了，是没开**）

## 跑法（必须用 -m，否则 import app 会失败）

    uv run python -m scripts.warmup_mcp            # 完整预热
    uv run python -m scripts.warmup_mcp --dry-run  # 只做 A/B 两段，不起进程（秒级）
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import pathlib
import subprocess
import sys
import time

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

DRY_RUN = "--dry-run" in sys.argv

# 只读工具的后缀白名单（**按语义挑，不写死具体工具名** —— 同 d27_verify 的零硬编码纪律）
READ_ONLY_SUFFIXES = ("_list", "_get", "_search", "_status", "_describe", "_schema", "_diagnose")

# 造参时的兜底值：D25 实测这个资源类型在本账号下**必然存在**（返回 total: 1），
# 且它**不需要 project 上下文**（实测代价：拿 enum[0] 的 activity_timeline 会被服务端拒）。
# ⚠ 它是**参数值**不是工具名 —— 零硬编码纪律管的是工具名。
#   取值时它**优先于 schema 里的 `enum[0]`**（enum 的顺序不代表推荐程度，见 `_sample_args`）。
FALLBACK_RESOURCE_TYPE = "organization"

# ★ 「schema 说可选、服务端说必填」的参数名单（**实测得来，不是猜的**）。
#   2026-10-08 实测：`harness_list` 的 `resource_type` **不在 `required` 里**，
#   但服务端硬要求 —— 不给就返回
#       {"error":"resource_type is required. Provide it explicitly or via a Harness URL."}
#   → 于是"按 schema 造参数"会造出 `{}`，然后被服务端拒绝。
#   ⚠ 这条坑的本质：**schema 不是完整的约束说明书**，它只是"官方声明的"那一部分。
#     凡依赖 schema 做自动化的地方，都要为"声明之外的约束"留兜底。
_ALWAYS_FILL = ("resource_type",)

# 本脚本自己的进程是否会出现在 pgrep 结果里（命令行含 "harness-mcp-v2" 才需要排除）
_SELF_PATTERN = "harness-mcp-v2"


# ------------------------------------------------------------------ 基础设施
PASS = 0
FAIL = 0
SKIP = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    """记录一条断言。**不用 assert** —— 让全部断言都跑完再给结论（D26 规矩）。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  |  {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  |  {detail}" if detail else ""))
    return cond


def skip(name: str, why: str) -> None:
    """缺前提就跳过并明说 —— **不要崩在半路**（D26 规矩）。"""
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name}  |  {why}")


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def ms(t: float) -> str:
    return f"{t * 1000:.0f}ms" if t < 1 else f"{t:.2f}s"


# ------------------------------------------------------------------ A 缓存
def _cached_packages() -> list[dict]:
    """
    扫 `~/.npm/_npx/*/node_modules/harness-mcp-v2/package.json`。

    为什么不用 `npm ls` / `npx --no-install`：那两者**都会联网或改缓存**，
    而这一段的要求是"**不起进程、不联网**，只看本地有什么"。
    """
    home = pathlib.Path(os.environ.get("HOME") or pathlib.Path.home())
    pattern = str(home / ".npm" / "_npx" / "*" / "node_modules" / "harness-mcp-v2" / "package.json")
    found: list[dict] = []
    for raw in glob.glob(pattern):
        pkg = pathlib.Path(raw)
        try:
            info = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 读不懂的缓存条目直接忽略，不算错误
            continue
        root = pkg.parents[2]          # .../_npx/<hash>
        found.append(
            {
                "hash": root.name,
                "version": info.get("version"),
                "root": str(root),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(root.stat().st_mtime)),
                "size": _dir_size(root),
            }
        )
    return sorted(found, key=lambda d: d["mtime"], reverse=True)


def _dir_size(path: pathlib.Path) -> str:
    """目录大小（best-effort：du 不在就返回 '?'，**不许因为它失败就崩掉整段**）。"""
    try:
        r = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.split()[0]
    except Exception:  # noqa: BLE001
        pass
    return "?"


def section_A() -> list[dict]:
    head("A 缓存：本地 npm 缓存里有没有 harness-mcp-v2？（不起进程、不联网）")
    cached = _cached_packages()
    if cached:
        for c in cached:
            print(f"  · 版本 {c['version']}  |  {c['size']}  |  落地时间 {c['mtime']}  |  {c['hash']}")
        print(f"  · 共 {len(cached)} 条：版本集合={sorted({c['version'] for c in cached})}")
        print("  ⚠ `_npx/<hash>` 的目录名由**命令字符串**算出来 —— 改了 spec（如 @latest → @3.2.33）"
              "就会换目录，")
        print("     于是「本地有没有」不能只看这里；**真正的判据在 C 段的 T1 耗时**"
              "（几十秒内起不来 = 在拉包）。")
    else:
        print("  · 本地缓存里**没有** harness-mcp-v2 —— 第一次起会现拉包（分钟级）")
    return cached


# ------------------------------------------------------------------ B 配置
def section_B() -> dict:
    head("B 配置：当前生效的 harness 参数是什么？")
    from app.config import settings

    cfg = {
        "enabled": settings.mcp_harness_enabled,
        "command": settings.mcp_harness_command,
        "args": settings.mcp_harness_args,
        "registry": settings.mcp_npm_registry,
        "read_only": settings.mcp_harness_read_only,
        "auto_approve_risk": settings.mcp_harness_auto_approve_risk,
        # ⚠ 铁律 6：PAT **只报有没有**，绝不打印内容
        "key_set": bool(settings.harness_api_key),
    }
    for k, v in cfg.items():
        print(f"  · {k:20s} = {v}")
    return cfg


# ------------------------------------------------------------------ 工具挑选
def _sample_args(schema: dict) -> dict | None:
    """
    按工具**自报的** JSON Schema，为它现造一组合法参数。

    除了 `required`，还会把 `_ALWAYS_FILL` 里那些"**schema 说可选、服务端却硬要**"
    的参数一并补上（见 `_ALWAYS_FILL` 上面的实测依据）。

    造不出来的返回 `None`（调用方会换下一个候选工具）——
    这正是铁律 2 附则③「实验脚本不能含你要证明的东西」的落地：
    参数来自 server 的 schema，不是我写死的。
    """
    props = schema.get("properties") or {}
    wanted = list(schema.get("required") or [])
    for extra in _ALWAYS_FILL:
        if extra in props and extra not in wanted:
            wanted.append(extra)

    args: dict = {}
    for p in wanted:
        pspec = props.get(p) or {}
        if p == "resource_type":
            # ⚠ 这一支**必须排在 enum 前面** —— 实测教训（2026-10-08）：
            #   schema 给的 enum 有 125+ 个值，而 `enum[0]` 只是**字母序第一个**
            #   （`activity_timeline`）；它需要 project 上下文，服务端直接拒绝：
            #       "activity_timeline: listing requires project scope (org_id + project_id)"
            #   → 改用 D25 实测过、本账号下**必然存在**且不需要 project scope 的 organization。
            #   ★ 一般规律：**enum 的顺序不表达"推荐程度"**，别拿 [0] 当默认值。
            args[p] = FALLBACK_RESOURCE_TYPE
        elif pspec.get("enum"):
            args[p] = pspec["enum"][0]
        elif pspec.get("default") is not None:
            args[p] = pspec["default"]
        else:
            return None
    return args


def _pick_readonly(tools: list[dict]) -> tuple[str, dict] | None:
    """
    从**注册表里的 harness 工具**中挑一个只读的，并造好参数。

    为什么从注册表取而不是重新 `tools/list`：注册表里的 `parameters` 正是
    `tools/list` 给的那份 schema（原样透传，见 `MCPClient.discover_and_register`），
    而它同时是 **LLM 真正看到的东西** —— 从它出发更贴近真实使用路径。

    排序规则：**后缀优先级优先**（`_list` 最干净），同级再按必填参数个数。
    ⚠ 不能反过来"参数少的优先" —— 实测教训：`harness_list` 的 `resource_type`
      不在 `required` 里，按"参数少优先"会挑出 `{}` 然后被服务端拒绝；
      按后缀优先 + `_ALWAYS_FILL` 补参，才能组出一次**真正会成功**的调用。

    挑不出返回 None —— 调用方会 SKIP 而不是崩（D26 规矩）。
    """
    order = {s: i for i, s in enumerate(READ_ONLY_SUFFIXES)}
    cands = []
    for t in tools:
        nm = t.get("name") or ""
        rank = next((order[s] for s in READ_ONLY_SUFFIXES if nm.endswith(s)), None)
        if rank is None:
            continue                                  # 非只读（create/update/delete/execute）→ 排除
        args = _sample_args(t.get("parameters") or {})
        if args is None:
            continue                                  # 造不出合法参数 → 换一个
        cands.append((rank, len(args), nm, args))
    if not cands:
        return None
    cands.sort()
    _, _, nm, args = cands[0]
    print(f"  ↳ 选定只读工具：{nm}  参数={args}  （从 {len(tools)} 个 harness 工具里按"
          f"『只读后缀 + 参数可造』挑的，**没有写死名字**）")
    return nm, args


# ------------------------------------------------------------------ 报错定性
# 决定一条工具调用失败**该不该当成"账号没了"**。
# 现场教训（2026-10-08 第一次跑）：E 段红了，红字写着「真心调 harness_list 拿到了数据」失败 ——
#   字面看像"账号/PAT 失效"，实际是**我自己造的参数不对**（schema 没声明 resource_type 必填）。
#   判据必须落在代码里，不能靠人去猜（铁律 10「报警先怀疑检查器」）。
_ERROR_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 认证放最前：它的关键词更"具体"，被别的规则抢走的风险最小
    ("认证/账号", ("401", "403", "unauthorized", "forbidden", "api key", "pat",
                   "expired", "authentication", "account identifier")),
    ("参数（取证方式问题）", ("is required", "requires", "invalid", "missing", "must be",
                              "unexpected", "scope", "pass both")),
    ("网络/限流", ("timeout", "temporary", "rate limit", "429", "econn", "socket")),
)


def _classify(text: str) -> str:
    """给错误文本定性。定性不了就返回「未知」—— **不许硬塞进某一类**。"""
    low = (text or "").lower()
    for label, signs in _ERROR_SIGNS:
        if any(s in low for s in signs):
            return label
    return "未知"


# ------------------------------------------------------------------ 孤儿进程
def _pids_matching(pat: str) -> set[int] | None:
    """
    用 `pgrep -f` 扫进程 —— **返回 None 表示"读不到"**，不是"没有"。

    ⚠ 判据（铁律 9）：「我这条命令没拿到」≠「这东西不存在」。
      pgrep 不可用/被沙箱拦时，这一整段只能 SKIP，**不许当成"无孤儿"**。
    """
    try:
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode == 1:      # pgrep: 无匹配（这是明确的"没有"）
        return set()
    if r.returncode != 0:
        return None
    out: set[int] = set()
    for line in r.stdout.split():
        try:
            out.add(int(line))
        except ValueError:
            continue
    return out - {os.getpid()}     # 别把自己算进去


def _describe_pids(pids: set[int]) -> str:
    return ", ".join(str(p) for p in sorted(pids)) if pids else "（无）"


# ------------------------------------------------------------------ C/D/E harness
async def warm_harness() -> dict:
    """把 harness 真起一遍，量三个数并真调一次。返回结果字典。"""
    from app.config import settings
    from app.mcp import MCPClient, build_specs
    from app.mcp.manager import spec_timeout

    specs = [s for s in build_specs(settings) if s.name == "harness"]
    if not specs:
        return {"ok": False, "reason": "no_harness_spec"}
    spec = specs[0]

    result: dict = {"ok": False}

    # ---- 基线：起进程**之前**的 harness 相关进程 ----
    before = _pids_matching(_SELF_PATTERN)
    if before is None:
        print("  （pgrep 不可用 → F 段的孤儿检查将 SKIP，不当成「无孤儿」）")
    else:
        print(f"  起进程前，命令行含「{_SELF_PATTERN}」的进程：{_describe_pids(before)}")

    client = MCPClient(spec)
    # ---- C 起进程 + 握手 ----
    head(f"C 起进程（T1）：起 {spec.command} 子进程并完成握手")
    print("  这是**最慢的一步**：首次要下包（分钟级），之后也要起 node 进程 + 加载模块。\n")
    t0 = time.perf_counter()
    connect_err: Exception | None = None
    try:
        await client.connect(timeout_s=spec_timeout(spec))
    except Exception as e:  # noqa: BLE001
        connect_err = e
    t1 = time.perf_counter() - t0
    # ⚠ 这条必须**由数据驱动**。第一版写的是 `check(..., True, ...)` —— 那是**恒真断言**，
    #   永远不会红（铁律 11：断言既要能假通过、也要能假失败）。
    check(
        "C1 连接成功（进程起来了、握手完成）",
        connect_err is None,
        f"T1={ms(t1)}"
        + (f"  {type(connect_err).__name__}: {connect_err}" if connect_err else ""),
    )
    if connect_err is not None:
        result.update(
            {"ok": False, "reason": "connect_failed", "t1_s": t1, "error": str(connect_err)}
        )
        await client.close()
        return result
    result["t1_s"] = t1
    print(f"  · server 自报  = {getattr(client.server_info, 'name', None)} "
          f"{getattr(client.server_info, 'version', None)}")
    print(f"  · 协议版本    = {client.protocol_version}")

    # ---- D tools/list ----
    head("D 发现（T2）：tools/list 能不能拿到工具清单？")
    t0 = time.perf_counter()
    try:
        names = await client.discover_and_register()
    except Exception as e:  # noqa: BLE001
        dt = time.perf_counter() - t0
        check("D1 tools/list 成功", False, f"{ms(dt)} {type(e).__name__}: {e}")
        result.update({"ok": False, "reason": "discover_failed", "error": str(e)})
        await client.close()
        return result
    t2 = time.perf_counter() - t0
    result["t2_s"] = t2
    result["tool_names"] = names
    check("D1 tools/list 成功", bool(names), f"T2={ms(t2)}（含注册 stub）共 {len(names)} 个")
    for n in names:
        print(f"    · {n}")
    result["tool_count"] = len(names)
    if len(names) != 11:
        # PRD §17.3 第 3 项写的是 11 个。**只警告不判红** —— 上游可能改版，
        # 而"数字变了"与"接入坏了"是两件事（d27_verify 的 E7 才是那个硬断言）。
        print(f"    ⚠ PRD 检查清单写的是 11 个，这里是 {len(names)} 个"
              f"（可能是上游改版，也可能是工具集被过滤 —— 演示前确认一下）")

    # ---- E 真调一次（★ 补挂起的死账）----
    head("E 调用（T3）★：真调一次只读工具 —— 补 2026-10-06 到期日之后的死账")
    print("（背景：10-07 复查只验了『连接 + tools/list』——都是**读**；"
          "**工具执行从没验过**。\n  『风险还没兑现』≠『风险已解除』，这一段就是来结这笔账的）\n")
    from app.tools.registry import describe_tools

    mine = [t for t in describe_tools() if t.get("source") == spec.name]
    picked = _pick_readonly(mine)
    if picked is None:
        skip("E1 真调一次只读工具",
             f"{len(mine)} 个 harness 工具里挑不出『只读 + 参数可造』的候选")
        result["call_ok"] = None
    else:
        full, args = picked
        # 注册表里的名字带 namespace 前缀；调远端时要把它剥掉（harness 的 namespace 是空串）
        ns = spec.namespace
        remote = full[len(ns) + 1:] if ns and full.startswith(ns + "_") else full
        t0 = time.perf_counter()
        out = await client.call(remote, args)
        t3 = time.perf_counter() - t0
        result["t3_s"] = t3
        result["call_tool"] = remote
        result["call_args"] = args
        result["call_out"] = out
        ok = bool(out) and "执行失败" not in out and "返回错误" not in out
        check(f"E1 ★ 真调 {remote}({args}) 拿到了数据", ok, f"T3={ms(t3)} 长度={len(out)}")
        print(f"    ↳ 返回前 200 字：{out[:200]!r}")
        if not ok:
            kind = _classify(out)
            print(f"    ⚠ 这条失败的定性：**{kind}**")
            if kind.startswith("认证"):
                print("      → 这才是那个真问题（TRIAL 账号 2026-10-06 到期）。演示前必须解决。")
            elif kind.startswith("参数"):
                print("      → 是**取证脚本自己的参数没造对**，不是 harness 坏了"
                      "（「报警先怀疑检查器」）→ 修 `_sample_args`，别去改 harness。")
            else:
                print("      → 定性不了，把上面那段原文一起看。")
        result["call_ok"] = ok
        result["call_error_kind"] = None if ok else _classify(out)

    # ---- F 回收 ----
    head("F 回收：断开之后有没有留下孤儿 node 进程？")
    t0 = time.perf_counter()
    await client.close()
    close_s = time.perf_counter() - t0
    check("F1 断开在限时内完成（关闭流程不被拖住）", close_s < 15.0, f"{close_s:.2f}s / 限时 15s")

    await asyncio.sleep(1.0)      # 给子进程一点退出的时间（EOF → 自行退出）
    after = _pids_matching(_SELF_PATTERN)
    if after is None or before is None:
        skip("F2 无孤儿进程", "pgrep 读不到（『拿不到』不等于『没有』，不许当绿灯）")
        result["orphans"] = None
    else:
        leaked = after - before
        check("F2 ★ 断开后没留下孤儿进程（生命周期归宿主）",
              not leaked, f"残留 pid={_describe_pids(leaked)}")
        result["orphans"] = sorted(leaked)

    result["ok"] = True
    return result


# ------------------------------------------------------------------ G 库存体检
async def check_inventory() -> dict:
    head("G 顺带体检：本机自研 inventory server 在不在？（毫秒级 → **不预热**，只确认）")
    from app.config import settings
    from app.mcp import MCPClient, build_specs

    specs = [s for s in build_specs(settings) if s.name == "inventory"]
    if not specs:
        skip("G1 inventory 接入", "配置里没有 inventory spec")
        return {"ok": None}
    client = MCPClient(specs[0])
    t0 = time.perf_counter()
    try:
        await client.connect(timeout_s=20.0)
        names = await client.discover_and_register()
    except Exception as e:  # noqa: BLE001
        check("G1 inventory 接入成功", False, f"{type(e).__name__}: {e}")
        await client.close()
        return {"ok": False, "error": str(e)}
    dt = time.perf_counter() - t0
    check("G1 inventory 接入成功（耗时说明它为什么不需要预热）",
          bool(names), f"{ms(dt)} 共 {len(names)} 个工具 → {names}")
    await client.close()
    return {"ok": True, "tool_count": len(names), "connect_s": dt}


# ------------------------------------------------------------------ H 报告
def section_H(cfg: dict, cached: list[dict], harness: dict, inventory: dict) -> int:
    head("H 汇总报告")
    print(f"  · 版本锁定      : {cfg['args']}")
    if cached:
        print(f"  · 本地缓存      : {len(cached)} 条，版本 "
              f"{sorted({c['version'] for c in cached})}，最大 {cached[0]['size']}")
    else:
        print("  · 本地缓存      : 无（首次起要拉包，分钟级）")
    print(f"  · PAT           : {'已配置' if cfg['key_set'] else '★ 未配置'}")
    print(f"  · 只读开关      : read_only={cfg['read_only']} auto_approve_risk={cfg['auto_approve_risk']}")

    t1 = harness.get("t1_s")
    if t1 is None:
        print("  · T1 起进程+握手: —")
    else:
        # ★ 这条判据才是"缓存到底命中没有"的答案（目录名是命令字符串的哈希，看目录猜不出来）
        if t1 < 15:
            hint = "→ 缓存命中，没在拉包"
        elif t1 < 60:
            hint = "→ 偏慢，可能在做版本解析或首次解包"
        else:
            hint = "★ → 在现拉包/首次加载，演示前必须跑过这一遍"
        print(f"  · T1 起进程+握手: {ms(t1)}  {hint}")
    print(f"  · T2 tools/list : {ms(harness['t2_s']) if 't2_s' in harness else '—'}"
          f"{'  工具数=' + str(harness.get('tool_count')) if harness.get('tool_count') else ''}")

    if "t3_s" in harness:
        verdict = "成功" if harness.get("call_ok") else f"★ 失败（定性：{harness.get('call_error_kind')}）"
        print(f"  · T3 真调用     : {ms(harness['t3_s'])}  "
              f"{harness.get('call_tool')}{harness.get('call_args')}  → {verdict}")
    elif harness.get("call_ok") is None and harness.get("ok"):
        print("  · T3 真调用     : —（挑不出合适的只读工具，见 E 段 SKIP）")
    else:
        print("  · T3 真调用     : —（没走到）")

    # ⚠ 三态要分清：**没起进程** / **起了但读不到进程表** / **起了且确认无残留**。
    #   把前两者都印成"未判定"会误导人以为"起过进程但没查清"。
    if not harness.get("ok"):
        orp_txt = "—（本次没起进程）"
    else:
        orp = harness.get("orphans")
        orp_txt = "无" if orp == [] else ("未判定（pgrep 读不到）" if orp is None else str(orp))
    print(f"  · 孤儿进程      : {orp_txt}")

    if inventory.get("ok") is None:
        print("  · inventory     : —（本次没跑这一段）")
    elif inventory.get("ok"):
        print(f"  · inventory     : 在，{inventory.get('tool_count')} 个工具，"
              f"{ms(inventory.get('connect_s', 0))}")
    else:
        print(f"  · inventory     : ★ 不在（接入失败：{inventory.get('error')}）")

    print(f"\n  断言：PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
    if FAILED:
        for n in FAILED:
            print(f"    ✗ {n}")

    # ---- 退出码（由数据决定，不是硬编码 —— 铁律 11）----
    if not cfg["enabled"]:
        print("\n  退出码 2：**前置不满足** —— 配置里 harness 开关是关的（MCP_HARNESS_ENABLED=false）。")
        print("          预热的前提就是打开它；请先打开再跑（或改用 --dry-run 只看缓存）。")
        return 2
    if not cfg["key_set"]:
        print("\n  退出码 2：**前置不满足** —— .env 里没有 HARNESS_API_KEY。")
        print("          不是坏了，是没配；配好之后重跑。")
        return 2
    if FAIL == 0 and harness.get("ok"):
        print("\n  退出码 0：预热成功 —— 演示可以开始了。")
        return 0
    print("\n  退出码 1：**预热失败** —— 演示会翻车，先修这个再演示。")
    print("          排障入口：脚本里打印的错误、以及子进程 stderr 落盘路径"
          "（MCPClient.stderr_path）。")
    return 1


# ------------------------------------------------------------------ main
async def main() -> int:
    print("AgentForge · MCP 预热")
    print(f"项目根：{PROJECT_ROOT}")
    print(f"模式  ：{'--dry-run（只查缓存与配置，不起进程）' if DRY_RUN else '完整预热'}")

    cached = section_A()
    cfg = section_B()

    if DRY_RUN:
        head("H 汇总报告（dry-run）")
        print(f"  · 版本锁定：{cfg['args']}")
        print(f"  · 本地缓存：{str(len(cached)) + ' 条 ' + str(sorted({c['version'] for c in cached})) if cached else '无'}")
        print(f"  · PAT     ：{'已配置' if cfg['key_set'] else '未配置'}")
        print("\n  （--dry-run：不连接、不调用，故不给退出码语义，一律返回 0）")
        return 0

    if not cfg["enabled"]:
        head("跳过 C~G：配置里 harness 开关是关的")
        print("  预热的前提是打开 harness。当前 MCP_HARNESS_ENABLED=false。")
        return section_H(cfg, cached, {}, {})

    if not cfg["key_set"]:
        head("跳过 C~F：没有 PAT 就无法预热 harness")
        print("  （G 段仍会跑：inventory 是本机 server，不需要 PAT）")
        inventory = await check_inventory()
        return section_H(cfg, cached, {}, inventory)

    harness = await warm_harness()
    inventory = await check_inventory()
    return section_H(cfg, cached, harness, inventory)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
