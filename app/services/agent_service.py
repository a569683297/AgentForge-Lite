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

import json
import uuid
from typing import Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.citation import check_citations, get_sources, reset_sources
from app.core.logging import logger
from app.schemas.chat import ChatResult, SourceItem
from app.services.llm_gateway import chat_with_tools

# D28：观测层唯一出口（span 命名规范、根 span、trace 级属性都从这里取）
from app.services.observability import SPAN_ROOT, span, tool_span_name, trace_attrs
from app.tools import execute_tool, get_tools_schema  # 注册表：工具能力集中管理
from app.tools.registry import get_tool_source


# 单轮对话内最多走多少个节点（LangGraph 的 recursion_limit；不传则是库默认值 25）。
# 依据 PRD v4.1 §9.2 定为 12：多步下钻最多 4 轮、共 9 个节点（plan/execute ×4 + 收尾），留 3 个余量；
# 超限时 LangGraph 抛 GraphRecursionError。
# ⚠ 修复前这里只在注释里写着"=10"，代码里从未真正传给 LangGraph —— 实际生效的是库默认值 25，
#   注释与行为不符。现在显式声明，不让它跟着库默认值漂。
RECURSION_LIMIT = 12


# ---- State：所有节点共享的状态（类比 pinia store）----
class AgentState(TypedDict):
    messages: list[dict]        # 完整对话（user/assistant/tool 消息）
    step_count: int             # 已循环步数
    # D15：长期摘要（PRD §9.4），由 run_agent 从 PG 读入，plan_node 拼进 system。
    # NotRequired：其它调用方（历史验证脚本）构造 state 时不传它也不会报错 ——
    # plan_node 用 state.get("summary") 取值，缺了就是"没有更早的记忆"。
    summary: NotRequired[str | None]


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


def build_system_prompt(summary: str | None = None) -> str:
    """
    拼 plan 节点的 system prompt（D15：PRD §9.4 的「组装顺序」）。

    顺序：人设（PLAN_SYSTEM_PROMPT）在前、**摘要**在后。

    摘要为什么放 system，而不是往 messages 里插一条：
      ① 插进 messages 会**伪装成一条真实对话轮次** —— LLM 会以为"我说过这句话"，
         干扰它对轮次和引用编号的判断
      ② 会产生**多条 system 消息**，而各家厂商对多 system 的兼容性参差

    摘要为什么放在人设**之后**（而不是最前面）：它是"更早的记忆"，
    越靠近当前对话的上下文越容易被用上；人设是指令，排最前更稳。
    """
    if not summary:
        return PLAN_SYSTEM_PROMPT
    return (
        f"{PLAN_SYSTEM_PROMPT}\n\n"
        f"【更早对话的摘要】（本次会话更早期的内容，供参考）\n{summary}"
    )


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
    result = await chat_with_tools(
        [{"role": "system", "content": build_system_prompt(state.get("summary"))}]
        + messages,
        get_tools_schema(),   # ← 从注册表取（D8：不再内联）
        trace_name=trace_name,
    )
    # LLM 返回的完整 assistant 消息（可能含 tool_calls 数组；也可能就是最终回答）
    assistant_msg = result["message"]
    return {"messages": messages + [assistant_msg]}


# ---- 节点 2：execute（执行工具）+ observe（结果写回）----
async def execute_node(state: AgentState) -> AgentState:
    """执行 assistant 消息里声明的 tool_calls，结果以 tool 消息写回。

    D8 变化：不再直接查 TOOLS dict + 自己 try/except，
    改为调注册表的 execute_tool（它内部处理未知工具/执行失败）。
    """
    messages = state["messages"]
    last = messages[-1]  # plan 追加的 assistant 消息（含 tool_calls）
    tool_calls = last.get("tool_calls") or []

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
                level="ERROR" if ("执行失败" in out or "未知工具" in out) else "DEFAULT",
            )

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
) -> ChatResult:
    """
    带记忆对话的完整入口：读历史 → 跑 Agent → 双层写回 → 返回回答 + 来源。

    D11 变化：返回值从 str 变成 ChatResult（answer + sources + invalid_citations）。
    D14 变化：① session_id 从 str 改为 uuid.UUID；② 新增 PG 全量落库（第 ⑧ 步）。
    D22 变化：① 新增 persist 开关（评测模式不写会话数据）；
              ② 返回值新增 tool_calls（本轮工具调用序列）。

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
    from app.services.session_service import append_messages, get_summary

    # ⓪ 引用收集器必须在跑图之前重新绑定。
    #    ContextVar 复制的是「绑定」不是「对象内容」，不重新绑定会读到上个请求的残留。
    reset_sources()

    # ① 读历史（Redis 优先，未命中/不可用走 PG 兜底）
    history = await get_window(session_id)

    # ①.5 读长期摘要（D15）。首次对话时 sessions 行还没建 → 返回 None，
    #     build_system_prompt 会据此跳过这一段，不会多出一条空的"摘要"。
    summary = await get_summary(session_id)

    # ② 组装状态（历史 + 本轮提问；摘要单独走 state.summary 进 system）
    state: AgentState = {
        "messages": history + [{"role": "user", "content": user_input}],
        "step_count": 0,
        "summary": summary,
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
    with trace_attrs(session_id=session_id, trace_name=SPAN_ROOT):
        with span(
            SPAN_ROOT,
            as_type="agent",
            input={"user_input": user_input},
            metadata={"persist": persist},
        ) as root_span:
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
            await append_messages(session_id, new_messages, title_hint=user_input)
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

    # ⑨ 返回结构化结果
    logger.info(
        "Agent 完成 session=%s 回答长度=%d 来源数=%d 工具调用=%s 越界引用=%s persist=%s",
        session_id,
        len(final_answer),
        len(sources),
        tool_names or "无",
        invalid or "无",
        persist,
    )
    return ChatResult(
        answer=final_answer,
        sources=sources,
        invalid_citations=invalid,
        tool_calls=tool_names,
    )
