"""
Agent 引擎（D6 版本：LangGraph ReAct 单 Agent，官方 function calling）
========================================================================
把"思考→行动→观察→再思考"的 Agent 循环建模成状态机图。

端到端流程（每轮循环）：
plan（LLM 决策 + 回答，走官方 function calling）
  → 条件边判断：
     有 tool_calls → execute（执行工具，结果以 tool 消息写回）→ 回 plan
     无 tool_calls → END（plan 这一步输出的就是最终回答）

D14 修复（plan 抢答）：
  原图是 plan → (execute | answer)，两个节点各调一次 LLM。实测发现：不需要工具时，
  plan 的 LLM **已经把答案说完了**，条件边又走 answer 再调一次 LLM 重新生成一遍 ——
  一轮白烧一次调用，PG 里留下两条内容相近的 assistant（实测 messages 表每轮都成对）。
  修法：把 answer 节点的引用规范**并进 plan 的 prompt**，让 plan 同时承担"决策"与
  "回答"，无 tool_calls 时直接 END。LLM 调用数降到理论最少：
      无工具轮 1 次（原 2 次）／有工具轮 2 次（原 3 次）

消息协议（OpenAI 兼容，必须遵守）：
assistant 消息带 tool_calls → tool 消息带 tool_call_id 关联返回
（缺失 tool_call_id 会 400，这是今天踩的坑）

类比（你确认过的）：State=pinia 全局状态 / Node=页面或hooks /
Edge=固定路由 / 条件边=动态路由
"""

import contextvars
import json
import uuid
from typing import AsyncIterator, Literal, NotRequired, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.core.citation import check_citations, get_sources, reset_sources
from app.core.logging import logger
from app.core.tenant import owner_scope
from app.core.metric_results import get_metric_results, reset_metric_results
from app.schemas.chat import ChatResult, MetricResultItem, SourceItem
from app.services.llm_gateway import chat_with_tools, chat_with_tools_stream

# D28：观测层唯一出口（span 命名规范、根 span、trace 级属性都从这里取）
from app.services.observability import (
    SPAN_ROOT,
    current_trace_id,
    span,
    tool_span_name,
    trace_attrs,
)
from app.tools import execute_tool, get_tools_schema  # 注册表：工具能力集中管理
from app.tools.registry import get_tool_source


# 单轮对话内最多走多少个节点（LangGraph 的 recursion_limit；不传则是库默认值 25）。
# 依据 PRD v4.1 §9.2 定为 12：多步下钻最多 4 轮、共 9 个节点（plan/execute ×4 + 收尾），留 3 个余量；
# 超限时 LangGraph 抛 GraphRecursionError。
# ⚠ 修复前这里只在注释里写着"=10"，代码里从未真正传给 LangGraph —— 实际生效的是库默认值 25，
#   注释与行为不符。现在显式声明，不让它跟着库默认值漂。
RECURSION_LIMIT = 12


# ---- 流式开关（M6 优化②）----
# "当前这次跑图，要不要把过程实时推给下游"—— plan / execute 节点据此决定：
# 走流式通道 + 发事件（True），还是维持改造前的原路径（False）。
#
# 为什么用 ContextVar（"随执行上下文自动传递的变量"）而不是给节点传参：
#   ① 节点签名是 LangGraph 的契约（state → state），加参数要改一层传递链；
#   ② ContextVar 的语义恰好就是"这次请求的上下文"—— 在 astream 之前 set、
#      finally reset；节点协程由 asyncio 复制上下文时自动继承。
#      （实测三种场景：ainvoke 里读 False / astream 里读 True / reset 后回 False）
# ⚠ 它只影响"发不发事件、走不走流式通道"，**不改变图的结构与状态** ——
#   同一份 state 走两条路径，产物字段逐字段一致（这是"流式不影响业务"的机制保证）。
_STREAMING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "agent_streaming", default=False
)


# ---- State：所有节点共享的状态（类比 pinia store）----
class AgentState(TypedDict):
    messages: list[dict]        # 完整对话（user/assistant/tool 消息）
    step_count: int             # 已循环步数
    # D15：长期摘要（PRD §9.4），由 run_agent 从 PG 读入，plan_node 拼进 system。
    # NotRequired：其它调用方（历史验证脚本）构造 state 时不传它也不会报错 ——
    # plan_node 用 state.get("summary") 取值，缺了就是"没有更早的记忆"。
    summary: NotRequired[str | None]
    # 用户画像（2026-10-21）：**跨会话**稳定的事实与偏好，由 run_agent 读好后渲染成文本。
    # 与 summary 的区别就一句话：summary 是"这个会话刚才聊了什么"，
    # profile 是"这个人是谁"。前者换会话就没了，后者换会话仍然成立。
    # NotRequired：评测/验证脚本构造 state 时不传 → 缺省 = 不带画像（零影响）。
    profile: NotRequired[str | None]
    # S2（模型切换）：本轮选中的模型 id（内置通道名 / 自定义 UUID），由 run_agent 注入。
    # NotRequired：评测/验证脚本构造 state 时不传 → 缺省 = 现有默认链路（零影响）。
    model_id: NotRequired[str | None]
    # S2：本轮**实际执行**的模型身份（网关在每次调用成功后回填，plan_node 转存）。
    # 出口是 ChatResult.model_used / done 帧的 model_used（消息归因"本轮由 XX 回答"）。
    model_used: NotRequired[dict | None]


# ---- 节点 1：plan（LLM 决策 + 最终回答）----
# D14 修复：原来这是"纯决策"节点，回答由 answer 节点负责；现在两件事合并到一次调用。
# 这段 prompt 是**两个 prompt 合并**的产物 —— 前半是原 plan 的工具决策，
# 后半是原 answer 的引用规范（逐条搬过来，一条都没删，否则 D11 的引用标注会退化）。
PLAN_SYSTEM_PROMPT = (
    "你是一个 AI 助手。根据用户问题决定下一步：\n"
    "如果需要公司内部资料（制度、项目、人名、数据等）才能回答，就调用工具查询，"
    "可以多轮调用、逐步下钻。\n"
    "如果不需要查（闲聊、常识），或上面的工具结果已经足够，"
    "就直接给出最终回答 —— 这段输出会原样展示给用户，之后不会再被改写。\n"
    "回答要求：\n"
    "1. 基于工具结果回答时，每条来自资料的论断后必须标注来源编号，"
    "格式为 [1]、[2]（对应工具结果里的 [n]）。\n"
    "2. 只允许使用工具结果中确实存在的编号，不要编造编号；"
    "也不要写出工具结果里没有的内容。\n"
    "3. 工具结果不足以回答时，直接说明资料中没有相关信息，不要自行推测。\n"
    "4. 不要提及工具调用的技术细节（函数名、参数、数据库等）。"
)


def build_system_prompt(summary: str | None = None, profile: str | None = None) -> str:
    """
    拼 plan 节点的 system prompt（D15：PRD §9.4 的「组装顺序」）。

    顺序：**人设 → 用户画像 → 会话摘要**（三段，任一段为空就跳过）。

    为什么摘要放 system，而不是往 messages 里插一条：
      ① 插进 messages 会**伪装成一条真实对话轮次** —— LLM 会以为"我说过这句话"，
         干扰它对轮次和引用编号的判断
      ② 会产生**多条 system 消息**，而各家厂商对多 system 的兼容性参差

    为什么三段的相对位置是这个顺序（2026-10-21 加画像时定的）：
      · 人设排最前：它是指令，在最前面最稳。
      · **画像排在摘要之前**：画像是"跨会话稳定的背景"（你是谁），
        摘要是"本会话临时发生过什么"（刚才聊了什么）。先稳定后临时 ——
        模型按这个顺序读，不会把"我上周说过我是前端"和"刚才聊到一半的话题"混在一起。
        反过来（摘要在前）实测更容易出现"用一段临时上下文盖掉长期设定"的漂移。
      · 摘要在最后：它最长，且越靠近当前对话越容易被用上。

    画像为什么**也**放 system 而不是插 messages：与摘要同理，而且画像里没有"轮次"语义，
    插进去会变成一句莫名其妙的 user 消息，模型可能把它当成本轮的新输入来回答。
    """
    blocks = [PLAN_SYSTEM_PROMPT]
    if profile:
        blocks.append(profile)
    if summary:
        blocks.append(f"【更早对话的摘要】（本次会话更早期的内容，供参考）\n{summary}")
    return "\n\n".join(blocks)


async def plan_node(state: AgentState) -> AgentState:
    """LLM 看当前对话，决定下一步：调工具，还是直接给出最终回答。

    ⚠ D14 修复后这个节点的输出**可能直接就是给用户的最终答案** ——
    没有 tool_calls 时条件边直接 END，不会再有任何节点改写它。
    """
    messages = state["messages"]
    # Langfuse 观测区分：合并之后"决策"和"作答"都发生在这一个节点里，
    # 靠节点名已经分不出来了，改用 trace_name 区分 —— 判断依据是"这一轮是否已经拿到过工具结果"。
    trace_name = (
        "agent-answer"
        if any(m.get("role") == "tool" for m in messages)
        else "agent-plan"
    )
    prompt_messages = (
        [
            {
                "role": "system",
                "content": build_system_prompt(
                    state.get("summary"), state.get("profile")
                ),
            }
        ]
        + messages
    )
    # S2：本轮选中的模型（None = 默认链路）。锁定/解析逻辑都在网关 ——
    # 节点层只负责透传，不在这里做任何判断（否则"选了什么"会有第二个真相）。
    model_id = state.get("model_id")
    if _STREAMING.get():
        # 流式分支：走流式通道，边收边把 content 增量推给下游（SSE 出口）。
        # writer 是 LangGraph 的 custom 流出口：非流式运行时它是空操作（实测），
        # 所以这里的调用本身不会给非流式路径带来任何行为变化。
        writer = get_stream_writer()
        result = await chat_with_tools_stream(
            prompt_messages,
            get_tools_schema(),   # ← 从注册表取（D8：不再内联）
            trace_name=trace_name,
            on_content=lambda text: writer({"type": "token", "text": text}),
            model_id=model_id,
        )
        assistant_msg = result["message"]
        # 中间轮的"预告文本"（如"我来查一下"）已经漏给了前端 —— 这一轮最终走向
        # 工具调用，那段文本不属于最终回答，补一帧 reset 让前端清掉：
        # 保证"屏幕上的文字"最终 == done 里的 answer，而不是半截预告 + 正式回答拼接。
        # （实测 DeepSeek 调工具时 content 恒为空、这帧不会触发；属防御性封边。）
        if assistant_msg.get("tool_calls") and assistant_msg.get("content"):
            writer({"type": "reset"})
    else:
        # ↓ 非流式分支：与改造前**逐字一致** —— run_agent / 评测 / 回归零影响
        result = await chat_with_tools(
            prompt_messages,
            get_tools_schema(),   # ← 从注册表取（D8：不再内联）
            trace_name=trace_name,
            model_id=model_id,
        )
        assistant_msg = result["message"]
    # LLM 返回的完整 assistant 消息（可能含 tool_calls 数组；也可能就是最终回答）
    # S2：把**实际执行**的模型身份转存进 state（出口在 run_agent / done 帧）。
    # 每次都覆盖：同一轮内各次调用通常同通道；若中途换过（将来），
    # 记录的是最后一次的实际执行者 —— 归因要的是"最终是谁答的"。
    return {"messages": messages + [assistant_msg], "model_used": result.get("model_used")}


# ---- 节点 2：execute（执行工具）+ observe（结果写回）----
def _is_tool_failure(output: str) -> bool:
    """
    工具输出是否算"失败"——span 的 ERROR 判定与流式 end 事件的 `ok` 用**同一判定**。

    表达式与改造前 span 里的那行**逐字一致**（不加不减）：
    两条出口若各写一份，改一处忘一处就会出现"卡片显示成功、trace 标红"（或反过来）
    —— 同一个事实两个真相。抽出来是为了保证它们将来也不会分叉。
    """
    return "执行失败" in output or "未知工具" in output


async def execute_node(state: AgentState) -> AgentState:
    """执行 assistant 消息里声明的 tool_calls，结果以 tool 消息写回。

    D8 变化：不再直接查 TOOLS dict + 自己 try/except，
    改为调注册表的 execute_tool（它内部处理未知工具/执行失败）。
    """
    messages = state["messages"]
    last = messages[-1]  # plan 追加的 assistant 消息（含 tool_calls）
    tool_calls = last.get("tool_calls") or []

    # 流式：本节点负责"工具开始/结束"两个事件（前端据此把卡片从「运行中」推进到结果态）
    writer = get_stream_writer() if _STREAMING.get() else None

    tool_messages = []
    for tc in tool_calls:
        # D28：工具名必须在 try **外面**先取到 —— span 的名字依赖它。
        # 取值用 `.get` 链而不是 `tc["function"]["name"]`：名字提到 try 外之后，
        # 就必须自己保证它不会抛（原代码的取名在 try 内，结构异常会被吞成
        # "解析失败"；换个位置而不换写法，就会把一个可恢复的失败升级成节点崩溃）。
        fn_name = (tc.get("function") or {}).get("name") or "unknown"

        # D28：MCP 来源标识 —— 验收②「能指出某次调用来自 harness」的地基。
        # `source` 对 MCP 工具就是 server 名（"harness" / "inventory"），本地工具是 "local"。
        # 本地工具**不该谎称**来自某台 MCP server，所以映射成 None
        # （PRD F8.4 要的是"这一次是外部 server 的调用，不是自研工具"）。
        source = get_tool_source(fn_name)
        mcp_server_name = source if source != "local" else None

        # 流式：执行**开始前**推 start —— 前端即时显示「正在调用 xxx」，
        # 这正是"Agent 过程可见"的落点（工具可能要跑 1~3 秒，这才是用户真正在等的时间）
        if writer:
            writer({"type": "tool", "name": fn_name, "phase": "start"})

        with span(
            tool_span_name(fn_name),
            as_type="tool",
            # 三个字段一起写：回答"调了哪个具体工具"（tool_name）
            # 与"它属于哪台 server"（mcp_server_name / source）。
            # ⚠ 只能放 metadata —— tool 属 span-like 类型，
            #   带不了 model / usage 那几个字段（传了也会静默丢弃）。
            metadata={
                "tool_name": fn_name,
                "mcp_server_name": mcp_server_name,
                "source": source,
            },
        ) as tool_span:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                # 交给注册表执行（内部已处理未知工具/异常，返回字符串结果）
                # D10：注册表改 async 后，这里必须 await——否则拿到的是 coroutine 对象
                result = await execute_tool(fn_name, args)
            except Exception as e:
                args = {}          # 给 span 的 input 一个确定值，避免 UnboundLocalError
                result = f"工具调用解析失败: {e}"

            out = str(result)
            # 失败也要留在 trace 上（与 rerank 的"降级可归因"同一条原则）：
            # 只写日志的话，"这台 server 的工具最近一直失败"这件事
            # 在 Langfuse 上是看不见的 —— 而它恰恰是运维最该看见的。
            tool_span.update(
                input=args,
                output=out,
                level="ERROR" if _is_tool_failure(out) else "DEFAULT",
            )

        # 流式：执行完推 end（ok 与上面 span 的判定同一启发式 —— 见 _is_tool_failure）
        if writer:
            writer({"type": "tool", "name": fn_name, "phase": "end", "ok": not _is_tool_failure(out)})

        # tool 消息必须带 tool_call_id 关联（否则 400）
        tool_messages.append(
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(result),
            }
        )
    return {"messages": messages + tool_messages}


# ---- 条件边判断：plan 之后走 execute 还是收工 ----
def should_continue(state: AgentState) -> Literal["execute", "end"]:
    """看 plan 的 LLM 输出：有 tool_calls 就去执行；否则它已经把话说完了 → END。

    D14 修复：原来这里返回 "answer"，多走一个节点再调一次 LLM。现在没有 answer 节点了——
    plan 的输出就是最终回答，直接结束。
    """
    last = state["messages"][-1]
    return "execute" if last.get("tool_calls") else "end"


# ---- 组装图 ----
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)

    graph.add_edge(START, "plan")
    # plan 后：有工具调用 → execute；否则 plan 的输出就是最终回答 → END
    graph.add_conditional_edges(
        "plan", should_continue, {"execute": "execute", "end": END}
    )
    # execute 后：把工具结果带回 plan（基于结果作答；需要下钻时会在这里再次决定调工具）
    graph.add_edge("execute", "plan")

    return graph.compile()


# 模块级实例（懒编译）
_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_graph()
    return _agent


# ============================================================
# 带记忆的 Agent 入口（D8 新增，D11 改返回结构）
# ============================================================
# 注：引用校验 check_citations 放在 app/core/citation.py
#     （纯函数、不依赖 config，可与收集器一起单独测试）
async def run_agent(
    session_id: uuid.UUID,
    user_input: str,
    *,
    persist: bool = True,
    model_id: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> ChatResult:
    """
    带记忆对话的入口（薄包装）。

    多租户（2026-10-21）：**只做一件事** —— 在整段执行期间把"当前用户"
    放进上下文，让检索链路（由 LLM 触发的工具调用）能读到它。
    真正的工作在 `_run_agent_impl`，参数逐一透传。

    为什么包装而不是在每个函数上加 owner 参数：
      检索发生在 `execute_tool → search_documents` 里，而那一跳的调用者是
      **LLM**（它只会填 query）。用户身份必须是环境信息，不能是参数 ——
      详见 `app/core/tenant.py` 文件头。
    """
    with owner_scope(owner_id):
        return await _run_agent_impl(
            session_id,
            user_input,
            persist=persist,
            model_id=model_id,
            owner_id=owner_id,
        )


async def _run_agent_impl(
    session_id: uuid.UUID,
    user_input: str,
    *,
    persist: bool = True,
    model_id: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> ChatResult:
    """
    带记忆对话的完整入口：读历史 → 跑 Agent → 双层写回 → 返回回答 + 来源。

    D11 变化：返回值从 str 变成 ChatResult（answer + sources + invalid_citations）。
    D14 变化：① session_id 从 str 改为 uuid.UUID；② 新增 PG 全量落库（第 ⑧ 步）。
    D22 变化：① 新增 persist 开关（评测模式不写会话数据）；
              ② 返回值新增 tool_calls（本轮工具调用序列）。
    S2 变化：① 新增 model_id（用户选中的模型：内置通道名或自定义 UUID）。
                不传 = 现有默认链路逐字不变；传了 = 锁定该模型、失败报错不静默换 ——
                解析失败抛 ModelResolutionError，接口层翻成 422；
              ② 返回值新增 model_used（**实际执行**的模型，见 schemas.ModelUsed）。

    ------------------------------------------------------------------
    persist=False 的语义与边界（评测专用）
    ------------------------------------------------------------------
    评测要对 50 条题 × 3 个配置各跑一遍。若照常写回，会往 messages 里灌
    150 个**假会话** —— 而 PRD §10 说 messages 是"用户对话"表，评测不是用户对话。
    明细表 eval_case_results 已经是这些答案的唯一真相，messages 里的副本
    只会与它慢慢漂移，却看起来也像"数据"。

    ⚠ 但它**影响不到本轮答案** —— 这不是推测，是代码结构保证的：
      写操作（⑦⑧⑧.5）全部排在 ④ 取答案之后，改不了已经生成的那段文本。
      唯一的例外是"读到自己刚写的东西"，而评测每条题都新建 session，
      单次运行内不会发生。**所以这个前提必须由调用方保证**（runner 每条题
      新建 session_id），否则第二题会读到第一题的问答，答案就真的变了。

    关掉写入后 **仍然执行** ① get_window / ①.5 get_summary：
      新 session 必然读到空，看似可以跳过 —— 但保留意味着**只有一条读路径**。
      "评测走另一条读路径"会让"评测测的是不是线上那条路径"这个问题永远无法回答。

    Args:
        session_id: 会话 ID（uuid.UUID —— 与 sessions.id / messages.session_id 同类型）
        user_input: 用户本轮输入
        persist:    True=写回会话记忆（正常对话）；False=不写（评测）。默认 True，旧行为不变。
    Returns:
        ChatResult：回答文本 + 来源映射表 + 越界引用编号 + 工具调用序列
    """
    from app.services.memory_service import append_turn, get_window, maybe_summarize
    from app.services.profile_service import render_for_prompt
    from app.services.session_service import append_messages, get_summary

    # ⓪ 引用收集器必须在跑图之前重新绑定。
    #    ContextVar 复制的是「绑定」不是「对象内容」，不重新绑定会读到上个请求的残留。
    #    （D37 块 3：指标结果收集器同族同理，并排重置。）
    reset_sources()
    reset_metric_results()

    # ① 读历史（Redis 优先，未命中/不可用走 PG 兜底）
    history = await get_window(session_id)

    # ①.5 读长期摘要（D15）。首次对话时 sessions 行还没建 → 返回 None，
    #     build_system_prompt 会据此跳过这一段，不会多出一条空的"摘要"。
    summary = await get_summary(session_id)

    # ①.6 读用户画像（2026-10-21）。**读在跑图之前、与摘要同一位置** ——
    #     它要进本轮 system prompt，晚一步就没机会了。
    #     ⚠ 内部是 fail-open 的（读不到 → None → 不拼这一段），见 profile_service 文件头。
    #     为什么传 owner_id 而不是 user 对象：画像归属就是 users.id，
    #     传整个 User 会让"用哪个字段做归属"出现第二个说法。
    profile_block = await render_for_prompt(owner_id, user_input)

    # ② 组装状态（历史 + 本轮提问；摘要与画像单独走 state 进 system）
    state: AgentState = {
        "messages": history + [{"role": "user", "content": user_input}],
        "step_count": 0,
        "summary": summary,
        "profile": profile_block,   # None = 这个人没有画像 / 画像读不到
        "model_id": model_id,   # S2：None = 默认链路
    }

    # ③ 跑图 + ④~⑥.5 取结果（D28：这一段就是「用户等待的全部」，包进**一条 trace**）
    #
    #   ⚠ 范围刻意**到 ⑥.5 为止，不含下面的 ⑦⑧ 写回**：写回是副作用，不占用户等待时间。
    #     把它算进根 span 的 latency，会让"这一轮慢在哪"失真 ——
    #     而 D28 之后这个 latency 是要被当指标读的（D35 的 latency_p95）。
    #
    #   ⚠ `trace_attrs` 必须在**根 span 之外** —— 官方硬约束："Pre-existing spans
    #     will NOT be retroactively updated"。实测把根 span 建在它之前时，
    #     根 span 的 session/user 是 None 而子 span 有值 ——
    #     那种"看起来设了、其实只设了一半"的状态最难查。
    # 本轮 trace id：**只能在 span 内部取** —— `current_trace_id()` 读的是当前
    # 上下文里的活动 span，出了 with 就没了。但落库排在 span 之后（写回是副作用，
    # 不占"用户等待"的延迟），所以先存到局部变量带出去。
    # 埋点 SDK 未配置（no-op）时这里是 None → messages.trace_id 写 NULL →
    # Trace 页如实显示"该次提问早于 trace 记录"，不报错、也不编造。
    trace_id: str | None = None
    with trace_attrs(session_id=session_id, trace_name=SPAN_ROOT):
        with span(
            SPAN_ROOT,
            as_type="agent",
            input={"user_input": user_input},
            metadata={"persist": persist},
        ) as root_span:
            trace_id = current_trace_id()

            # ③ 跑图（recursion_limit 见文件顶部常量说明）
            result = await get_agent().ainvoke(
                state, config={"recursion_limit": RECURSION_LIMIT}
            )

            # ④ 取最终回答
            #   注意：中间会有带 tool_calls 的 assistant 消息（content 可能为空，也可能只有一句
            #   "我来查一下"），所以要从后往前找"第一条 content 非空的 assistant 消息"。
            #   D14 修复后 plan 输出的就是最终答案，这条规则依然成立、而且更稳：
            #   不会再出现"plan 抢答 + answer 正式答"两条并列、需要从里面挑一条的情况。
            final_answer = ""
            for msg in reversed(result["messages"]):
                if msg["role"] == "assistant" and msg.get("content"):
                    final_answer = msg["content"]
                    break

            # ⑤ 取出本请求累计的来源（结构通道）
            raw_sources = get_sources()
            sources = [SourceItem(**item) for item in raw_sources]

            # ⑤.5 取出本请求的指标查询结果（D37 块 3：结构通道之二）。
            #      与 sources 同族同位置 —— 两个收集器都在跑图后一次性取出，
            #      出口分别是 ChatResult 的两个字段（非流式）/ done 帧（流式）。
            raw_metric_results = get_metric_results()
            metric_results = [MetricResultItem(**item) for item in raw_metric_results]

            # ⑥ 校验引用编号有没有越界（幻觉引用检测）
            invalid = check_citations(final_answer, len(sources))
            if invalid:
                logger.warning(
                    "回答引用了不存在的来源编号 session=%s 越界编号=%s 实际来源数=%d",
                    session_id,
                    invalid,
                    len(sources),
                )

            # ⑥.5 收集本轮实际调用的工具序列（D22 新增）——
            #      评测判「C 类题有没有选对工具」（tool_miss）的**唯一依据**。
            #      这件事从答案文本里看不出来：答案可能答对，却完全没查库。
            #      与 D11 的 sources 同族 ——「过程的结构化产物需要一条出口」。
            #      「本轮新增了哪些消息」= 跑完图后的完整列表减去进图之前那一段；
            #      可以直接按长度切，因为 history 是进图前的全部内容，图只会在后面追加。
            new_messages = result["messages"][len(history):]
            tool_names = [
                (call.get("function") or {}).get("name", "")
                for message in new_messages
                if message.get("role") == "assistant"
                for call in (message.get("tool_calls") or [])
            ]
            tool_names = [name for name in tool_names if name]

            # S2：本轮实际执行的模型（plan_node 转存；归因用）。
            # 从图状态取而不是某个局部变量 —— 它跨节点而活（plan/execute 都经历过）。
            model_used = result.get("model_used")

            # 根 span 的 output = 这一轮的最终产物。
            # 记 answer 之外**也记 tool_calls**：一次调用里"答了什么"和
            # "查了哪些库"是两件事，前者可能对而后者完全没查（D22 的 tool_miss 正是这个）。
            root_span.update(
                output={"answer": final_answer, "tool_calls": tool_names}
            )

    # ---- 以下三步是「写回会话记忆」；评测模式（persist=False）整体跳过 ----
    #      跳过是安全的：它们在时间上全部排在 ④ 取答案之后，改不了已生成的文本。
    #      详细边界见函数 docstring 的 persist 说明。
    if persist:
        # ⑦ 写回 Redis 热窗口（只存 user + 最终回答，不存中间工具消息——见 memory_service 说明）
        await append_turn(session_id, user_input, final_answer)

        # ⑧ 写回 PG 全量（D14 新增）：含中间的 assistant(tool_calls) 与 tool 消息。
        try:
            # trace_id 只落到本轮那条 user 消息上 —— Trace 页靠它反查"这次提问问了什么"
            await append_messages(
                session_id,
                new_messages,
                title_hint=user_input,
                trace_id=trace_id,
                owner_id=owner_id,
            )
        except Exception:
            # 落库失败不阻断对话：用户已经等到回答了，此时抛 500 只会让这一轮白跑，
            # 而且历史仍在 Redis 里（下一轮上下文不丢）。但必须留下 ERROR 日志 ——
            # 静默失败才是真正的坑：D12 的孤儿切片就是这么攒出来的。
            logger.exception("PG 落库失败（对话结果仍已返回）session=%s", session_id)

        # ⑧.5 摘要压缩检查（D15 新增 / PRD F5.1 后半 + F5.2）：
        #      排在 ⑧ 之后，是因为它的触发判据要从 PG 数轮数 ——
        #      必须等本轮的 user/assistant 先落库，否则永远差一轮。
        #      排在 ⑨ 之前（同步执行）：换来可预测的行为与可断言的结果；
        #      将来若要降延迟，可换成 asyncio.create_task，函数本身不用改。
        try:
            await maybe_summarize(session_id)
        except Exception:
            # 同上：摘要失败不影响本轮（回答已产出、记忆已写）。
            # maybe_summarize 内部已经把 LLM 失败/空输出都收敛成返回值了，
            # 这里兜的是"它自己崩了"（比如 PG 查询异常）—— 一样不该让对话 500。
            logger.exception("摘要压缩检查失败（对话不受影响）session=%s", session_id)

        # ⑧.6 画像抽取（2026-10-21）：与 ⑧.5 并列，但**不 await** —— 它是后台任务。
        #      为什么摘要同步、画像异步：
        #        摘要的结果**下一轮立刻要读**（读不到就断记忆），所以本轮必须等它写完；
        #        画像的结果晚 30 秒生效，用户完全感知不到 —— 没有理由让他等。
        #      触发条件与冷却都在 maybe_extract 内部判断，这里只管"提交"。
        try:
            from app.services.profile_extractor import schedule_extract

            schedule_extract(owner_id, session_id)
        except Exception:
            # 连"提交后台任务"都失败（极罕见，如事件循环已关闭）也不该影响对话
            logger.exception("画像抽取调度失败（对话不受影响）session=%s", session_id)

    # ⑨ 返回结构化结果
    logger.info(
        "Agent 完成 session=%s 回答长度=%d 来源数=%d 工具调用=%s 越界引用=%s persist=%s 模型=%s",
        session_id,
        len(final_answer),
        len(sources),
        tool_names or "无",
        invalid or "无",
        persist,
        (model_used or {}).get("id") or "默认",
    )
    return ChatResult(
        answer=final_answer,
        sources=sources,
        invalid_citations=invalid,
        tool_calls=tool_names,
        # S2：实际执行模型（归因）。这里**必须显式传**——有默认值 None，
        # 漏传不报错、只是归因静默丢失（D36 的教训同源：默认值会吞掉漏传的字段）
        model_used=model_used,
        # D37 块 3：指标查询的结构化结果（同样必须显式传，理由同上）
        metric_results=metric_results,
    )


# ============================================================
# 流式入口（M6 优化②）：run_agent 的逐段镜像
# ============================================================
async def run_agent_stream(
    session_id: uuid.UUID,
    user_input: str,
    *,
    persist: bool = True,
    model_id: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> AsyncIterator[dict]:
    """
    流式对话入口（薄包装）。

    ⚠ 租户上下文为什么必须设**在生成器内部**（而不是接口层调它之前）：
      接口层 `chat_stream` 是"创建生成器 → 立刻 return StreamingResponse"，
      真正的迭代发生在**函数返回之后**。而 `owner_scope` 用的是 `with`，
      出了 with 就还原 —— 在接口层设，等于在流开始前就已经失效了，
      于是流式对话里的检索会以"无归属"跑（= 搜全库，且完全不报错）。
      写在这里，`with` 覆盖整个 `async for` 的迭代期，上下文才是有效的。
    """
    with owner_scope(owner_id):
        async for event in _run_agent_stream_impl(
            session_id,
            user_input,
            persist=persist,
            model_id=model_id,
            owner_id=owner_id,
        ):
            yield event


async def _run_agent_stream_impl(
    session_id: uuid.UUID,
    user_input: str,
    *,
    persist: bool = True,
    model_id: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> AsyncIterator[dict]:
    """
    流式版 run_agent：产出**结构化事件流**（服务层只产事件，SSE 编码是接口层的事）。

    事件协议（前后端对齐的唯一出处；接口层 app/api/chat.py 只做"事件 → SSE 帧"的编码）：
        {"type": "token", "text": "..."}      答案增量（逐段，可能一帧多段）
        {"type": "reset"}                     清掉当前气泡里的中间文本
                                              （模型中途说了话又改去调工具时的处置）
        {"type": "tool", "name": ..., "phase": "start"}
        {"type": "tool", "name": ..., "phase": "end", "ok": true|false}
        {"type": "done", "session_id", "answer", "sources",
                        "invalid_citations", "tool_calls", "model_used"}
                                              —— 字段与 ChatResponse 对齐

    与 run_agent 的关系（M6 优化② 的核心约束）：
        - 读历史、组装 state、跑图、取结果、引用校验、三层写回 —— **同一套逻辑的逐段镜像**
          （本函数 ⓪~⑨ 与 run_agent ⓪~⑨ 一一对应，连注释编号都对齐）；
        - 唯一机制差异：图用 `astream` 跑（能收到节点发的事件），最终状态从
          "ainvoke 的返回值"改为"收集末个 values 快照"；
        - **两条出口各自独立可测**：`POST /api/chat` 走 run_agent、`/api/chat/stream` 走本函数；
          ⚠ 代价：两处逻辑需同步维护 —— 改 run_agent 的任一编号步骤时，回来对照这里
          （S2 的 model_id 透传 / model_used 出参就是两边同步改的实例）。

    done 为什么在写回**之后**才发：前端收到 done 即会跳 URL / 刷新侧栏，此后任何
    刷新、重读都必须能看到本轮的完整落库版本；若先发 done 再写回，两者之间会存在
    一个"屏幕上有答案、历史里读不到"的窗口。
    """
    from app.services.memory_service import append_turn, get_window, maybe_summarize
    from app.services.profile_service import render_for_prompt
    from app.services.session_service import append_messages, get_summary

    # ⓪ 引用收集器重绑（同 run_agent：ContextVar 复制的是绑定不是对象，必须每请求重绑）
    #    （D37 块 3：指标结果收集器同族同理，并排重置。）
    reset_sources()
    reset_metric_results()

    # ① 读历史（Redis 优先，未命中/不可用走 PG 兜底）
    history = await get_window(session_id)

    # ①.5 读长期摘要（首次对话时 sessions 行还没建 → None，system 里不拼这一段）
    summary = await get_summary(session_id)

    # ①.6 读用户画像（镜像 run_agent；逐段对应，改一边要回来对照另一边）
    profile_block = await render_for_prompt(owner_id, user_input)

    # ② 组装状态（与 run_agent 相同：历史 + 本轮提问；摘要与画像单独走 state）
    state: AgentState = {
        "messages": history + [{"role": "user", "content": user_input}],
        "step_count": 0,
        "summary": summary,
        "profile": profile_block,
        "model_id": model_id,   # S2：None = 默认链路
    }

    stream_token = _STREAMING.set(True)
    # 同 run_agent：trace id 只能在 span 内取，落库在 span 之后用（见那里的说明）
    trace_id: str | None = None
    try:
        # ③~⑥.5 与 run_agent 同一段：用户等待的全部，包进一条 trace（写回不在此范围内）
        with trace_attrs(session_id=session_id, trace_name=SPAN_ROOT):
            with span(
                SPAN_ROOT,
                as_type="agent",
                input={"user_input": user_input},
                metadata={"persist": persist, "stream": True},
            ) as root_span:
                trace_id = current_trace_id()

                # ③ 流式跑图：custom 事件逐条产出；values 快照保留最后一个（= 最终状态）
                #    （双模式实测形状：`(mode, chunk)` 元组；values 覆盖全图每个超步）
                final_state: AgentState | None = None
                async for mode, chunk in get_agent().astream(
                    state,
                    config={"recursion_limit": RECURSION_LIMIT},
                    stream_mode=["custom", "values"],
                ):
                    if mode == "custom":
                        yield chunk
                    else:
                        final_state = chunk

                assert final_state is not None, "astream 未产出任何 values 快照，图运行异常"

                # ④~⑥.5 从最终状态取结果（逐段对应 run_agent 的 ④⑤⑥⑥.5）
                result_messages = final_state["messages"]
                final_answer = ""
                for msg in reversed(result_messages):
                    if msg["role"] == "assistant" and msg.get("content"):
                        final_answer = msg["content"]
                        break

                raw_sources = get_sources()
                sources = [SourceItem(**item) for item in raw_sources]

                # D37 块 3：指标查询结果（与 sources 同位置取出，出口是 done 帧）
                raw_metric_results = get_metric_results()
                metric_results = [MetricResultItem(**item) for item in raw_metric_results]

                invalid = check_citations(final_answer, len(sources))
                if invalid:
                    logger.warning(
                        "回答引用了不存在的来源编号 session=%s 越界编号=%s 实际来源数=%d",
                        session_id,
                        invalid,
                        len(sources),
                    )

                new_messages = result_messages[len(history):]
                tool_names = [
                    (call.get("function") or {}).get("name", "")
                    for message in new_messages
                    if message.get("role") == "assistant"
                    for call in (message.get("tool_calls") or [])
                ]
                tool_names = [name for name in tool_names if name]

                # S2：本轮实际执行的模型（plan_node 转存；done 帧归因用）
                model_used = final_state.get("model_used")

                root_span.update(
                    output={"answer": final_answer, "tool_calls": tool_names}
                )
    finally:
        # 无论正常结束 / 异常 / 客户端断开，开关都要复位 ——
        # 泄漏成 True 会让"下一次非流式跑图"误走流式通道（回归脚本就在同进程里跑）
        _STREAMING.reset(stream_token)

    # ---- ⑦⑧⑧.5 写回会话记忆（镜像 run_agent；persist=False 整体跳过）----
    #      刻意放在 span 之外：写回是副作用，不占"用户等待"的延迟（与 run_agent 同一约定）
    if persist:
        await append_turn(session_id, user_input, final_answer)

        try:
            await append_messages(
                session_id,
                new_messages,
                title_hint=user_input,
                trace_id=trace_id,
                owner_id=owner_id,
            )
        except Exception:
            logger.exception("PG 落库失败（流式结果仍已返回）session=%s", session_id)

        try:
            await maybe_summarize(session_id)
        except Exception:
            logger.exception("摘要压缩检查失败（对话不受影响）session=%s", session_id)

        # ⑧.6 画像抽取（镜像 run_agent，同样不 await）
        #      ⚠ 流式出口特别注意：这个调用发生在 done 事件**发出之前**，
        #      但它只是 `create_task`（微秒级），不会推迟 done 帧 ——
        #      如果哪天它被改成 await，流式对话的"答完即结束"就会被拖成"答完再等 1~2 秒"。
        try:
            from app.services.profile_extractor import schedule_extract

            schedule_extract(owner_id, session_id)
        except Exception:
            logger.exception("画像抽取调度失败（对话不受影响）session=%s", session_id)

    # ⑨ done 事件（字段与 ChatResponse 对齐；sources 先 model_dump 成可 JSON 的 dict ——
    #    接口层只做 SSE 编帧，不做数据转换）
    logger.info(
        "Agent 流式完成 session=%s 回答长度=%d 来源数=%d 工具调用=%s 越界引用=%s persist=%s 模型=%s",
        session_id,
        len(final_answer),
        len(sources),
        tool_names or "无",
        invalid or "无",
        persist,
        (model_used or {}).get("id") or "默认",
    )
    yield {
        "type": "done",
        "session_id": str(session_id),
        "answer": final_answer,
        "sources": [s.model_dump() for s in sources],
        "invalid_citations": invalid,
        "tool_calls": tool_names,
        # S2：实际执行模型（归因"本轮由谁回答"；前端据此在消息上显示）
        "model_used": model_used,
        # D37 块 3：指标查询的结构化结果（前端图表数据；F11.3 的结构通道出口）
        "metric_results": [m.model_dump() for m in metric_results],
    }
