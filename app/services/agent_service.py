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

import uuid
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.citation import check_citations, get_sources, reset_sources
from app.core.logging import logger
from app.schemas.chat import ChatResult, SourceItem
from app.services.llm_gateway import chat_with_tools
from app.tools import execute_tool, get_tools_schema  # 注册表：工具能力集中管理


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
        [{"role": "system", "content": PLAN_SYSTEM_PROMPT}] + messages,
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
        try:
            import json

            fn_name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            # 交给注册表执行（内部已处理未知工具/异常，返回字符串结果）
            # D10：注册表改 async 后，这里必须 await——否则拿到的是 coroutine 对象
            result = await execute_tool(fn_name, args)
        except Exception as e:
            result = f"工具调用解析失败: {e}"
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
async def run_agent(session_id: uuid.UUID, user_input: str) -> ChatResult:
    """
    带记忆对话的完整入口：读历史 → 跑 Agent → 双层写回 → 返回回答 + 来源。

    D11 变化：返回值从 str 变成 ChatResult（answer + sources + invalid_citations）。
    D14 变化：① session_id 从 str 改为 uuid.UUID；② 新增 PG 全量落库（第 ⑧ 步）。

    端到端流程：
      ⓪ reset_sources()             重新绑定本请求的引用收集器（并发隔离的关键）
      ① get_window(session_id)      从 Redis 读最近 N 轮历史
      ② messages = 历史 + [本次提问]
      ③ agent.ainvoke(messages)     跑 LangGraph 的 ReAct 循环（plan 节点兼任"决策+回答"）
          └─ 工具执行时把来源登记进收集器（结构通道）
      ④ 取最终回答（plan 给出的那条；D14 修复后不再有"两份并列答案"要从里面挑）
      ⑤ get_sources()               取出本请求累计的来源
      ⑥ check_citations()           校验回答里的 [n] 有没有越界
      ⑦ append_turn(...)            写回 Redis 热窗口（只 user + assistant）
      ⑧ append_messages(...)        写回 PG 全量（含 tool 行）← D14 新增
      ⑨ 返回 ChatResult

    Args:
        session_id: 会话 ID（uuid.UUID —— 与 sessions.id / messages.session_id 同类型）
        user_input: 用户本轮输入
    Returns:
        ChatResult：回答文本 + 来源映射表 + 越界引用编号
    """
    from app.services.memory_service import append_turn, get_window
    from app.services.session_service import append_messages

    # ⓪ 引用收集器必须在跑图之前重新绑定。
    #    ContextVar 复制的是「绑定」不是「对象内容」，不重新绑定会读到上个请求的残留。
    reset_sources()

    # ① 读历史
    history = await get_window(session_id)

    # ② 组装状态（历史 + 本轮提问）
    state: AgentState = {
        "messages": history + [{"role": "user", "content": user_input}],
        "step_count": 0,
    }

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

    # ⑦ 写回 Redis 热窗口（只存 user + 最终回答，不存中间工具消息——见 memory_service 说明）
    await append_turn(session_id, user_input, final_answer)

    # ⑧ 写回 PG 全量（D14 新增）：含中间的 assistant(tool_calls) 与 tool 消息。
    #    「本轮新增了哪些消息」= 跑完图后的完整消息列表，减去进图之前的那一段。
    #    为什么可以直接按长度切：history 是进图前的全部内容，图只会在后面追加，
    #    所以 result["messages"][len(history):] 恰好是本轮新增
    #    （① 用户提问 → ② 若干中间步骤 → ③ 最终回答）。
    new_messages = result["messages"][len(history):]
    try:
        await append_messages(session_id, new_messages, title_hint=user_input)
    except Exception:
        # 落库失败不阻断对话：用户已经等到回答了，此时抛 500 只会让这一轮白跑，
        # 而且历史仍在 Redis 里（下一轮上下文不丢）。但必须留下 ERROR 日志 ——
        # 静默失败才是真正的坑：D12 的孤儿切片就是这么攒出来的。
        logger.exception("PG 落库失败（对话结果仍已返回）session=%s", session_id)

    # ⑨ 返回结构化结果
    logger.info(
        "Agent 完成 session=%s 回答长度=%d 来源数=%d 越界引用=%s",
        session_id,
        len(final_answer),
        len(sources),
        invalid or "无",
    )
    return ChatResult(answer=final_answer, sources=sources, invalid_citations=invalid)
