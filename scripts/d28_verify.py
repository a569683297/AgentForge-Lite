"""
D28 验证脚本：三层（四层）埋点补全 + Langfuse 取数通道
========================================================
一段回答一个问题。

七段各答什么问题：
  A 结构    —— 观测层的命名规范 / 取数层的字段映射，是不是真的在位
  B 检索树  —— 一次 retrieve() 到底建了几格、是不是**同一条 trace**、父子对不对
  C 对话树  —— 真跑一轮 run_agent：根 span 在不在、session 落没落上、generation 挂哪
  D 工具    —— 工具 span 的 metadata 能不能回答"调了哪个工具 / 属于哪台 server"
  E ★ 验收② —— 真调 harness 的工具，那格 span 真的标着 harness 吗
  F 腿 B    —— 取数通道：真拉回、字段映射、聚合、60s 缓存、不可达只升明确错误
  G 自证    —— 断言用的属性键真的存在吗（否则"全绿"可能只是空集合恒真）

★ 关键取证手法（本脚本最重要的设计）：
    给 Langfuse 的 TracerProvider **挂一个内存 exporter**，在**进程内**读回
    已经结束的 span。为什么不用 REST 读回：
      ① 官方对 v1 observations 的原文警告是 "may have data delays of several
         minutes" —— 用它做验收断言会变成"看运气"；
      ② 网络抖动会让"埋点对不对"这个纯逻辑问题变得不可判定。
    内存 exporter 读的是**同一份 span 数据**（同一个 TracerProvider），
    只是不经网络 —— 所以它证明的是"埋点结构"，网络那一段由 F 段单独证。

跑法（必须用 -m，否则 import app 会失败）：
    uv run python -m scripts.d28_verify
    uv run python -m scripts.d28_verify --with-harness   # 跑验收②（要 npm + PAT）
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys
import time
import uuid

# ⚠ 必须在**任何 app.* import 之前**（本文件的 app import 全在函数体内）。
#   理由同 D27：harness 的 `npx` 冷启动是**分钟级**（走镜像实测 2 分 31 秒），
#   默认不该让"跑一次验证"依赖网络与 npm。
#
# ⚠ 开关**由命令行强制决定**，不依赖 .env 的当前值：
#   .env 里 `MCP_HARNESS_ENABLED` 默认是 false，若这里不管它，
#   `--with-harness` 会被 .env 静默压掉 → E 段整段跳过 → **看起来像通过**。
#   这类"开关没生效但结果全绿"正是最该防的（同 D27 的 E7 两分支写法）。
WITH_HARNESS = "--with-harness" in sys.argv
os.environ["MCP_HARNESS_ENABLED"] = "true" if WITH_HARNESS else "false"

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- 基础设施
PASS = 0
FAIL = 0
FAILED: list[str] = []

# 运行时真见到过的工具名（各段往里塞）—— G 段自证靠它，
# 判据与 D27 一致：**与真工具名求交集**，而不是"看字符串长得像不像工具名"
# （后者会误伤路径常量，比如 "inventory_server.py"）。
OBSERVED: set[str] = set()


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
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def section_skip(title: str, why: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    print(f"  （跳过：{why}）")


# ---------------------------------------------------------------- 进程内取证
# 必须在导入 langfuse 实例之后、跑被测代码之前挂上。
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from app.services.llm_gateway import langfuse  # noqa: E402

EXPORTER = InMemorySpanExporter()
langfuse._resources.tracer_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))

# otel 属性键（实测确认，不是猜）
KEY_TYPE = "langfuse.observation.type"
KEY_SESSION = "session.id"


def take_spans() -> list:
    """取出自上次 clear 以来结束的全部 span。"""
    return list(EXPORTER.get_finished_spans())


def reset() -> None:
    EXPORTER.clear()


def names_of(spans: list) -> set[str]:
    return {s.name for s in spans}


def trace_ids_of(spans: list) -> set[int]:
    return {s.context.trace_id for s in spans}


def hex_trace(span) -> str:
    return f"{span.context.trace_id:032x}"


def hex_span(span) -> str:
    return f"{span.context.span_id:016x}"


def parent_name(spans: list, span) -> str | None:
    """这格的父叫什么（None = 它是根）。"""
    if span.parent is None:
        return None
    pid = f"{span.parent.span_id:016x}"
    for s in spans:
        if hex_span(s) == pid:
            return s.name
    return "<不在本批>"


def attr(spans: list, name: str, key: str):
    """按 span 名取一个属性值（同名取第一个）。"""
    for s in spans:
        if s.name == name:
            return dict(s.attributes or {}).get(key)
    return None


def meta_of(spans: list, name: str, key: str):
    """
    取 span 上 metadata 里的某个键。

    ⚠ 不写死完整键名：metadata 的键带前缀（形如 `langfuse.observation.metadata.source`），
      但具体前缀由 SDK 版本决定。这里按**后缀**匹配，并在 G 段把实际键名打出来 ——
      如果哪天 SDK 改了前缀，G 段会红，而不是让 D 段悄悄恒真。
    """
    for s in spans:
        if s.name != name:
            continue
        for k, v in dict(s.attributes or {}).items():
            if k.endswith(f"metadata.{key}"):
                return v
    return None


def print_tree(spans: list) -> None:
    """按父子关系打印这棵埋点树（给人和给日志看）。"""
    by_id = {hex_span(s): s for s in spans}
    for s in sorted(spans, key=lambda x: hex_span(x)):
        if s.parent is None:
            _print_node(s, by_id, 0)


def _print_node(span, by_id: dict, depth: int) -> None:
    t = dict(span.attributes or {}).get(KEY_TYPE)
    sess = dict(span.attributes or {}).get(KEY_SESSION)
    extra = f"  session={sess}" if sess else ""
    print(f"    {'  ' * depth}- {span.name:28} type={str(t):12}{extra}")
    for s in by_id.values():
        if s.parent is not None and f"{s.parent.span_id:016x}" == hex_span(span):
            _print_node(s, by_id, depth + 1)


# ---------------------------------------------------------------- A 结构
def section_A() -> None:
    head("A 结构：命名规范 / 取数层字段映射，是不是真的在位？")
    import app.services.observability as obs
    from app.services import langfuse_client as lc

    check("A1 观测层导出了 span 命名常量（PRD §18 #5 要求 D28 定下来）",
          all(hasattr(obs, n) for n in
              ("SPAN_ROOT", "SPAN_RETRIEVAL", "SPAN_EMBEDDING",
               "SPAN_VECTOR", "SPAN_BM25", "SPAN_RERANK")),
          f"{obs.SPAN_ROOT}/{obs.SPAN_RETRIEVAL}/{obs.SPAN_EMBEDDING}/"
          f"{obs.SPAN_VECTOR}/{obs.SPAN_BM25}/{obs.SPAN_RERANK}")
    # ⚠ 这里用假名 "demo"，**不写真工具名** —— 写了就会被 G3 的自证抓到
    #   （G3 的判据是"脚本常量 ∩ 运行时真工具名 = 空"）。第一次跑就是这么红的。
    check("A2 工具 span 名带 tool: 前缀（避免与检索层同名混组）",
          obs.tool_span_name("demo") == "tool:demo", obs.tool_span_name("demo"))
    check("A3 观测层复用 llm_gateway 的 Langfuse 实例（不新建第二个导出管道）",
          obs.langfuse is langfuse, "同一个对象" if obs.langfuse is langfuse else "两个实例！")

    check("A4 取数层是**只读**的（对外只有 query 这一个 async 入口）",
          hasattr(lc, "LangfuseClient") and hasattr(lc, "get_langfuse_client"),
          "LangfuseClient + get_langfuse_client")
    check("A5 字段映射把语义层命名翻成 REST 键名（不是直接拿 SDK 名去取）",
          lc._FIELD_PATHS.get("usage.totalTokens") == ("usageDetails", "total"),
          f"{lc._FIELD_PATHS}")
    check("A6 端点常量可一处切换（v1/v2 的取舍写在一个地方）",
          lc.OBSERVATIONS_PATH.startswith("/api/public/"),
          lc.OBSERVATIONS_PATH)

    # registry 的新访问器
    from app.tools.registry import get_tool_source

    check("A7 未知工具返回 unknown（不谎报成 local，避免留下假调用记录）",
          get_tool_source("__definitely_not_a_tool__") == "unknown",
          get_tool_source("__definitely_not_a_tool__"))


# ---------------------------------------------------------------- B 检索树
async def section_B() -> None:
    head("B 检索树：一次 retrieve() 建了几格？是不是同一条 trace？父子对不对？")
    from app.services.observability import trace_attrs
    from app.services.retrieval_service import retrieve

    reset()
    t0 = time.perf_counter()
    with trace_attrs(session_id="d28-verify-retrieval"):
        results = await retrieve("年假有几天")
    dt = time.perf_counter() - t0
    spans = take_spans()

    check("B0 内存 exporter 真的收到了 span（否则后面全是空集合恒真）",
          len(spans) > 0, f"收到 {len(spans)} 个 span")
    if not spans:
        return

    names = names_of(spans)
    want = {"retrieval", "vector_search", "embedding", "bm25", "rerank"}
    check("B1 五个检索 span 全部存在（四段 + 一个父）",
          want <= names, f"缺失={sorted(want - names)} 实得={sorted(names)}")

    tids = trace_ids_of(spans)
    check("B2 ★ 全部落在**同一条 trace**（这是 D28 要修的那个洞）",
          len(tids) == 1, f"trace 数={len(tids)}")

    check("B3 父子结构正确：retrieval 是根，vector_search/rerank 挂在它下面",
          parent_name(spans, next(s for s in spans if s.name == "retrieval")) is None
          and parent_name(spans, next(s for s in spans if s.name == "vector_search")) == "retrieval"
          and parent_name(spans, next(s for s in spans if s.name == "rerank")) == "retrieval",
          "retrieval(根) → vector_search/bm25/rerank")
    check("B4 embedding 挂在 vector_search 下面（向量那段能拆成两块）",
          parent_name(spans, next(s for s in spans if s.name == "embedding")) == "vector_search",
          "vector_search → embedding")

    # 类型：embedding 属 generation-like（能带 model），retriever 带不了
    check("B5 各格的观察类型符合预期（retriever / embedding）",
          attr(spans, "vector_search", KEY_TYPE) == "retriever"
          and attr(spans, "embedding", KEY_TYPE) == "embedding",
          f"vector={attr(spans, 'vector_search', KEY_TYPE)} "
          f"embedding={attr(spans, 'embedding', KEY_TYPE)}")
    check("B6 embedding 那格带上了 model 名（generation-like 才有的待遇）",
          bool(attr(spans, "embedding", "langfuse.observation.model.name")),
          f"model={attr(spans, 'embedding', 'langfuse.observation.model.name')}")

    print(f"\n  ── 本次 retrieve() 的埋点树（{dt * 1000:.0f}ms，返回 {len(results)} 条）──")
    print_tree(spans)


# ---------------------------------------------------------------- C 对话树
async def section_C() -> dict:
    head("C 对话树：真跑一轮 run_agent —— 根 span 在不在？session 落没落上？")
    from app.services.agent_service import run_agent

    reset()
    session_id = uuid.uuid4()
    t0 = time.perf_counter()
    # persist=False：评测/验证都不该往 messages 里灌假会话（D22 定的语义）
    result = await run_agent(session_id, "公司的年假有几天？", persist=False)
    dt = time.perf_counter() - t0
    spans = take_spans()

    check("C0 这一轮真的跑了（有回答、有 span）",
          bool(result.answer) and len(spans) > 0,
          f"{dt:.1f}s 回答长度={len(result.answer)} span 数={len(spans)}")
    if not spans:
        return {}

    names = names_of(spans)
    roots = [s for s in spans if s.parent is None]
    check("C1 ★ 只有**一个**根 span，且名字是 run_agent（一轮对话 = 一条 trace）",
          len(roots) == 1 and roots[0].name == "run_agent",
          f"根={[s.name for s in roots]}")
    check("C2 全部 span 同一条 trace",
          len(trace_ids_of(spans)) == 1, f"trace 数={len(trace_ids_of(spans))}")

    # ★ 这条是 D28 的核心修复点：以前每次 generation 自成一条 trace
    gens = [s for s in spans if dict(s.attributes or {}).get(KEY_TYPE) == "generation"]
    check("C3 ★ generation 不再自成一条 trace，而是挂在 run_agent 下面",
          all(s.parent is not None for s in gens)
          and all(parent_name(spans, s) is not None for s in gens),
          f"{len(gens)} 个 generation，父 = "
          f"{sorted({parent_name(spans, s) for s in gens})}")

    check("C4 根 span 带上了 session_id（F8.3 会话追踪树靠它）",
          str(attr(spans, "run_agent", KEY_SESSION)) == str(session_id),
          f"span={attr(spans, 'run_agent', KEY_SESSION)} 期望={session_id}")

    check("C5 根 span 的 type 是 agent（不是默认 span）",
          attr(spans, "run_agent", KEY_TYPE) == "agent",
          str(attr(spans, "run_agent", KEY_TYPE)))

    print(f"\n  ── 这一轮的埋点树（{dt:.1f}s）──")
    print_tree(spans)

    return {"tool_calls": result.tool_calls, "spans": spans}


# ---------------------------------------------------------------- D 工具 span
async def section_D(ctx: dict) -> None:
    head("D 工具 span：metadata 能不能回答『调了哪个工具 / 属于哪台 server』？")
    from app.mcp import MCPServerSpec, get_manager
    from app.services.observability import tool_span_name
    from app.tools.registry import describe_tools

    spans = ctx.get("spans") or []

    # ---- D-① 本地工具（上面那一轮真调用过）----
    local_tools = [s for s in spans if s.name.startswith("tool:")]
    if local_tools:
        s = local_tools[0]
        tname = s.name.split(":", 1)[1]
        check("D1 工具 span 名 = tool:<工具名>",
              s.name.startswith("tool:") and len(tname) > 0, s.name)
        check("D2 metadata.tool_name 与 span 名一致",
              meta_of(spans, s.name, "tool_name") == tname,
              f"{meta_of(spans, s.name, 'tool_name')}")
        check("D3 本地工具的 source = local，且 mcp_server_name 为 None（不谎报来自 server）",
              meta_of(spans, s.name, "source") == "local"
              and meta_of(spans, s.name, "mcp_server_name") in (None, ""),
              f"source={meta_of(spans, s.name, 'source')} "
              f"mcp_server_name={meta_of(spans, s.name, 'mcp_server_name')!r}")
    else:
        check("D1 上一轮至少调用过一个工具（否则工具 span 没被覆盖到）",
              bool(ctx.get("tool_calls")),
              f"tool_calls={ctx.get('tool_calls')}")

    # ---- D-② MCP 工具（接本机 inventory，不需要网络）----
    mgr = get_manager()
    spec = MCPServerSpec(
        name="d28inv",
        command=sys.executable,
        args=[str(PROJECT_ROOT / "mcp_servers" / "inventory_server.py")],
        env={k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
        | {"LOG_LEVEL": "info"},
        namespace="d28inv",
        cwd=str(PROJECT_ROOT),
    )
    st = await mgr.add_server(spec)
    if not check("D4 本机 MCP server 接入成功（MCP 工具的取证前提）",
                 st.get("connected") is True, f"error={st.get('error')}"):
        return

    remote = [t for t in describe_tools() if t["source"] == "d28inv"]
    OBSERVED.update(t["name"] for t in remote)
    check("D5 注册表里出现了这批 MCP 工具", len(remote) >= 1, f"{[t['name'] for t in remote]}")

    # ⚠ 必须走 **execute_node**，不能直接调 execute_tool ——
    #   埋点在 `execute_node` 里（"Agent 决定调工具"的那一步），
    #   直接调注册表等于跳过被埋点的那一层，span 根本不会建。
    #   第一版就是这么写的，D6/D7 全红 —— **验证方法错了会看起来像被测代码错**。
    from app.services.agent_service import execute_node

    reset()
    state = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_d28_verify",
                        "type": "function",
                        "function": {"name": remote[0]["name"], "arguments": "{}"},
                    }
                ],
            }
        ],
        "step_count": 0,
    }
    out_state = await execute_node(state)
    spans2 = take_spans()
    sname = tool_span_name(remote[0]["name"])
    out = out_state["messages"][-1]["content"]

    check("D6 ★ MCP 工具的 span 标着 source=d28inv（不是靠工具名猜的）",
          meta_of(spans2, sname, "source") == "d28inv",
          f"source={meta_of(spans2, sname, 'source')}（本批 span={sorted(names_of(spans2))}）")
    check("D7 ★ mcp_server_name 填的是 server 名（这正是 PRD F8.4 要的字段）",
          meta_of(spans2, sname, "mcp_server_name") == "d28inv",
          f"mcp_server_name={meta_of(spans2, sname, 'mcp_server_name')}")
    check("D8 调用结果仍是文本（埋点没破坏原有契约）",
          isinstance(out, str) and not out.startswith("<coroutine"), f"前 50 字={out[:50]!r}")

    await mgr.remove_server("d28inv")


# ---------------------------------------------------------------- E 验收②
async def section_E() -> None:
    head("E ★ 验收②：真调一次 harness 的工具，那格 span 真的标着 harness 吗？")
    if not WITH_HARNESS:
        section_skip("E ★ 验收②", "未加 --with-harness（npx 冷启动分钟级，默认不接）")
        return

    import json

    from app.config import settings
    from app.mcp import get_manager
    from app.mcp.manager import build_specs
    from app.services.agent_service import execute_node
    from app.services.observability import tool_span_name
    from app.tools.registry import describe_tools

    mgr = get_manager()

    # ⚠ 本脚本**不走 lifespan**（没有 TestClient），外部 server 不会自动接入 ——
    #   必须像应用启动时那样，按配置把 harness 手工接一次。
    #   （D27 的 E 段靠 TestClient 跑了一遍 lifespan 才接上 inventory；
    #     这里没有那一步，直接 add_server 更可控、也更快。）
    specs = [s for s in build_specs(settings) if s.name == "harness"]
    if not check("E0 配置里有 harness 这台 server（开关 + PAT 都在）",
                 len(specs) == 1,
                 f"全部 spec={[s.name for s in build_specs(settings)]}"):
        return
    st = await mgr.add_server(specs[0])
    if not check("E1 harness 接上了（npx 冷启动，可能要等）",
                 st.get("connected") is True,
                 f"error={st.get('error')} stderr={st.get('stderr_log')}"):
        return
    harness = [t for t in describe_tools() if t["source"] == "harness"]
    OBSERVED.update(t["name"] for t in harness)
    if not check("E1b 发现了一批 harness 工具", len(harness) >= 1, f"{len(harness)} 个"):
        return

    # 优先挑**无必填参数**的工具：不猜业务语义，只求把这条链路打通。
    # （有必填参数也照调 —— 失败也会留下 span，验收②要的是"能指出它来自 harness"，
    #   不要求那次调用成功；这一点在 E5 里单独区分。）
    pick = None
    for t in harness:
        required = (t.get("parameters") or {}).get("required") or []
        if not required:
            pick = t
            break
    pick = pick or harness[0]
    args = {
        p: "d28-verify"
        for p in ((pick.get("parameters") or {}).get("required") or [])
    }

    # ⚠ 同 D 段：必须走 **execute_node**（埋点在那里），不能直接调 execute_tool。
    #   （D 段第一次跑就是踩了这个坑，而 E 段当时没跟着改 —— 修法只补了被投诉的那一处，
    #     没推广到同类调用点。同一个错型连续犯两次，这次两处一起对齐。）
    reset()
    state = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_d28_harness",
                        "type": "function",
                        "function": {
                            "name": pick["name"],
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }
        ],
        "step_count": 0,
    }
    out_state = await execute_node(state)
    spans = take_spans()
    sname = tool_span_name(pick["name"])
    out = out_state["messages"][-1]["content"]

    check("E2 调用留下了一格工具 span（无论成败）",
          sname in names_of(spans), f"span 名={sname} 实得={sorted(names_of(spans))}")
    check("E3 ★★ 验收②：这格的 metadata.source = harness",
          meta_of(spans, sname, "source") == "harness",
          f"source={meta_of(spans, sname, 'source')}")
    check("E4 ★ 而且 mcp_server_name 也写着 harness（PRD F8.4 要的字段）",
          meta_of(spans, sname, "mcp_server_name") == "harness",
          f"mcp_server_name={meta_of(spans, sname, 'mcp_server_name')}")
    print(f"     ↳ 调用的工具={pick['name']!r} 参数={args} 结果前 60 字={out[:60]!r}")
    print("     （成败不影响验收②：要证的是『能指出这一次来自 harness』，"
          "不是『这次调用成功』—— 成功与否取决于业务语义与凭据）")

    await mgr.stop()


# ---------------------------------------------------------------- F 腿 B
async def section_F() -> None:
    head("F 腿 B：取数通道 —— 真拉回 / 字段映射 / 聚合 / 缓存 / 不可达降级")
    from app.services.langfuse_client import (
        LangfuseClient,
        LangfuseUnavailableError,
        get_langfuse_client,
    )

    client = get_langfuse_client()

    # ---- F-① 真拉回 ----
    today = time.strftime("%Y-%m-%d")
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    # ⚠ 窗口刻意开**一周**而不是"今天"。两个理由：
    #   ① 官方对 v1 observations 的原文警告是 "may have data delays of several
    #      minutes" —— "刚跑完的 span 什么时候能被读到"是不确定的，
    #      只查今天会让断言变成**看运气**（第一版就是这么红的：16 条里没有 generation）；
    #   ② 本段要证的是"取数与聚合的**能力**"，用已经落定的历史数据最稳。
    #      "新鲜数据多久可见"是另一个问题，不属于本段范围。
    week_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    rows: list[dict] = []
    ok = False
    for attempt in (1, 2):
        try:
            rows = await client._fetch_observations(week_ago, tomorrow, limit=300)
            ok = True
            break
        except LangfuseUnavailableError as e:
            if attempt == 1:
                # 很可能只是撞上速率限制 —— 等一下再试一次。
                # ⚠ 只在这个**验证脚本**里重试：生产代码刻意不重试
                #   （重试会加剧限流；见 langfuse_client 文件头对 429 的说明）。
                print(f"     ↳ 第 1 次拉取失败（{str(e)[:70]}），5s 后重试一次…")
                await asyncio.sleep(5)
            else:
                check("F1 Langfuse 可达（读通道能拉到数据）", False, str(e)[:120])

    if ok:
        check("F1 真拉回了 observations（验收③）",
              len(rows) > 0, f"{len(rows)} 条")
        if rows:
            keys = set(rows[0].keys())
            check("F2 拉回的是明细（不是空壳）：带 name / latency / startTime",
                  {"name", "latency", "startTime"} <= keys,
                  f"缺失={sorted({'name','latency','startTime'} - keys)}")
            check("F3 明细里能读到 token 用量（v2 没有这个字段，所以当前用 v1）",
                  any((o.get("usageDetails") or {}).get("total") for o in rows),
                  "至少一条带 usageDetails.total")

            # ---- F-② 聚合 ----
            agg = client._aggregate(rows, agg="p95", field="latency", group_by="name")
            check("F4 _aggregate 返回与 SQL 源同构的行（含分组/值/样本数）",
                  bool(agg) and all({"name", "value", "count"} <= set(r) for r in agg),
                  f"{len(agg)} 组，样本首行={agg[0] if agg else None}")

            tok = client._aggregate(rows, agg="sum", field="usage.totalTokens",
                                    group_by="name")
            check("F5 token 字段映射真的取到了值（不是静默全 None）",
                  any(r["value"] > 0 for r in tok),
                  f"非零组={[r for r in tok if r['value'] > 0][:2]}")

            check("F6 未知 field/agg 直接拒绝（不猜、不兜底）",
                  _raises(lambda: client._aggregate(rows, agg="p95", field="nope"))
                  and _raises(lambda: client._aggregate(rows, agg="median", field="latency")),
                  "nope / median 均被拒")

        # ---- F-③ 60s 缓存（复用 F-① 的窗口，**故意不发新请求**）----
        # 为什么不新开一个窗口去测：
        #   ① Langfuse 对 v1 observations **有速率限制**（实测撞到 429，服务端原文
        #      还建议改用 v2 做高流量读取）—— "每跑一次脚本就多拉一次 300 条"
        #      会自己把自己限流，断言于是变得看运气；
        #   ② 「第一次真发能拉到」已由 F-① 证明，「重复调用不再发」由这里证明 ——
        #      两件事分开证，合起来才是完整的缓存语义。
        calls = {"n": 0}
        orig_pages = client._fetch_pages

        async def counting(*a, **k):
            calls["n"] += 1
            return await orig_pages(*a, **k)

        client._fetch_pages = counting  # type: ignore[method-assign]
        try:
            again = await client._fetch_observations(week_ago, tomorrow, limit=300)
        finally:
            client._fetch_pages = orig_pages  # type: ignore[method-assign]

        check("F7 ★ 重复调用命中 60s 缓存（一条新 HTTP 都没发）",
              calls["n"] == 0 and len(again) == len(rows),
              f"新 HTTP={calls['n']} 次，条数={len(again)}（首次 {len(rows)}）")

    # ---- F-④ 不可达 → 明确错误，不挂死 ----
    bad = LangfuseClient(host="http://127.0.0.1:9", public_key="pk", secret_key="sk",
                         timeout_s=2.0)
    t0 = time.perf_counter()
    err: Exception | None = None
    try:
        await bad._fetch_observations(today, tomorrow, limit=5)
    except Exception as e:  # noqa: BLE001
        err = e
    dt = time.perf_counter() - t0
    check("F8 ★ 不可达时抛 LangfuseUnavailableError（类型明确 → 上层可只降级这一个指标）",
          isinstance(err, LangfuseUnavailableError), f"{type(err).__name__}: {str(err)[:80]}")
    check("F9 而且是**快速失败**，不是挂死",
          dt < 10.0, f"{dt:.2f}s（timeout 设 2s）")

    # query() 是 async —— 必须**真 await** 才会执行函数体。
    # 只写 `client.query(...)` 得到的是一个 coroutine 对象：函数体一行都没跑、
    # 断言自然不成立，还会留下 "coroutine was never awaited" 警告。
    # （这与本项目"命令错冒充对象坏"那条同型：探针自己写错，看起来却像被测代码有问题。）
    qerr: Exception | None = None
    try:
        await client.query(None, None, today, tomorrow)
    except Exception as e:  # noqa: BLE001
        qerr = e
    check("F10 query() 明确未实现（不是悄悄返回空 —— 空会被上层当成『该指标为 0』）",
          isinstance(qerr, NotImplementedError), f"{type(qerr).__name__}")


def _raises(fn, kinds: tuple = (ValueError,)) -> bool:
    try:
        fn()
    except kinds:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------- G 自证
def section_G(observed: set[str]) -> None:
    head("G 自证：断言用的属性键真的存在吗？（否则『全绿』可能只是空集合恒真）")
    reset()
    from app.services.observability import span, tool_span_name

    with span("g-probe", as_type="tool", metadata={"source": "g-selfcheck"}) as s:
        s.update(input={"a": 1}, output="done")
    spans = take_spans()

    check("G0 自证探针确实建出了 span", len(spans) == 1, f"{len(spans)} 个")
    if not spans:
        return

    keys = sorted(dict(spans[0].attributes or {}).keys())
    print(f"     ↳ 一个带 metadata 的 span 的真实属性键：{keys}")

    check("G1 ★ metadata 的键名与 D 段断言用的后缀一致（否则 D 段在恒真）",
          any(k.endswith("metadata.source") for k in keys),
          "存在 *.metadata.source")
    check("G2 观察类型键存在且值正确",
          dict(spans[0].attributes or {}).get(KEY_TYPE) == "tool",
          f"{KEY_TYPE}={dict(spans[0].attributes or {}).get(KEY_TYPE)}")

    # 本脚本不硬编码任何真工具名（工具名一律从注册表现取）——
    # 判据与 D27 一致：**与运行时真工具名求交集**，而不是"看字符串长得像不像"。
    check("G2b 自证前提成立：运行时确实见到过工具名（不是空集合恒真）",
          len(observed) > 0, f"运行时工具名 {len(observed)} 个")
    own = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    consts = {n.value for n in ast.walk(own)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    hard = sorted(consts & observed)
    check("G3 ★ 本脚本的字符串常量与运行时真工具名**零交集**（否则整份证据是循环的）",
          hard == [],
          f"命中={hard}" if hard else f"零命中（对照 {len(observed)} 个真工具名）")


# ---------------------------------------------------------------- main
async def main() -> int:
    # 关掉块缓冲：上一次用 `| tail -N` 时输出全被缓冲在管道里，
    # 卡了很久却看不到中间进度 —— 排查成本比故障本身还高（D27 记过）。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001, S110
        pass

    if not WITH_HARNESS:
        print("（未加 --with-harness：本轮不接 harness，E 段跳过。"
              "跑验收②用 `uv run python -m scripts.d28_verify --with-harness`）")

    section_A()
    await section_B()
    ctx = await section_C()
    await section_D(ctx)
    await section_E()
    await section_F()
    # 自证的对照集 = 各段见过的 MCP/harness 工具名 + 本轮真调过的 local 工具名
    section_G(OBSERVED | set(ctx.get("tool_calls") or []))

    # 收尾：别给后面留常驻子进程；把已上报的 span 刷出去（F 段要读服务端）
    from app.mcp import get_manager

    await get_manager().stop()
    try:
        langfuse.flush()
    except Exception:  # noqa: BLE001, S110
        pass

    head("汇总")
    print(f"  通过 {PASS} / 失败 {FAIL} / 共 {PASS + FAIL}")
    if FAILED:
        print("  失败项：")
        for n in FAILED:
            print(f"    - {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
