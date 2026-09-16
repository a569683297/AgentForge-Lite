"""
Agent 引擎（D6 版本：LangGraph ReAct 单 Agent，官方 function calling）
========================================================================
把"思考→行动→观察→再思考"的 Agent 循环建模成状态机图。

端到端流程（每轮循环）：
plan（LLM 决策：调工具 or 直接答，走官方 function calling）
  → 条件边判断：
     有 tool_calls → execute（执行工具）→ observe（结果写回）→ 回 plan
     无 tool_calls → answer（生成最终回答）→ END

消息协议（OpenAI 兼容，必须遵守）：
assistant 消息带 tool_calls → tool 消息带 tool_call_id 关联返回
（缺失 tool_call_id 会 400，这是今天踩的坑）

类比（你确认过的）：State=pinia 全局状态 / Node=页面或hooks /
Edge=固定路由 / 条件边=动态路由
"""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.citation import check_citations, get_sources, reset_sources
from app.core.logging import logger
from app.schemas.chat import ChatResult, SourceItem
from app.services.llm_gateway import chat, chat_with_tools
from app.tools import execute_tool, get_tools_schema  # 注册表：工具能力集中管理


# ---- State：所有节点共享的状态（类比 pinia store）----
class AgentState(TypedDict):
    messages: list[dict]        # 完整对话（user/assistant/tool 消息）
    step_count: int             # 已循环步数


# ---- 节点 1：plan（LLM 决策：调工具 or 直接答）----
async def plan_node(state: AgentState) -> AgentState:
    """LLM 看当前对话，决定下一步。返回的消息可能带 tool_calls。"""
    messages = state["messages"]
    system = {"role": "system", "content": "你是一个 AI 助手。根据用户问题决定是否需要调用工具。"}
    result = await chat_with_tools(
        [system] + messages,
        get_tools_schema(),   # ← 从注册表取（D8：不再内联）
        trace_name="agent-plan",
    )
    # LLM 返回的完整 assistant 消息（可能含 tool_calls 数组）
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


# ---- 节点 3：answer（生成最终回答）----
async def answer_node(state: AgentState) -> AgentState:
    """带着工具结果生成最终回答。

    D11 变化（引用通道 A：prompt）：明确要求 LLM 标注来源编号。
    之前的 prompt 只说"基于结果回答"，LLM 的默认倾向是"综合材料用自己的话答"
    ——结果就是 D10 那句话：上下文里有 [1]，但输出里没有。
    ⚠ 上一版最后那句"不要提及内部工具调用细节"很可能还在**主动抑制**它，
      所以这里改成"不要提技术细节，但必须标编号"，两件事分清。
    """
    messages = state["messages"]
    system = (
        "你是 AI 助手。\n"
        "如果上面有工具执行结果，请严格基于结果回答用户，"
        "并在每条来自资料的论断后标注来源编号，格式为 [1]、[2]（对应工具结果里的 [n]）。\n"
        "只允许使用工具结果中确实存在的编号，不要编造编号；"
        "也不要写出工具结果里没有的内容。\n"
        "如果工具结果不足以回答，就直接说明资料中没有相关信息，不要自行推测。\n"
        "不要提及工具调用的技术细节（函数名、参数、数据库等）。\n"
        "如果上面没有工具执行结果，直接回答用户。"
    )
    reply = await chat(
        [{"role": "system", "content": system}] + messages,
        trace_name="agent-answer",
    )
    return {"messages": messages + [{"role": "assistant", "content": reply}]}


# ---- 条件边判断：plan 之后走 execute 还是 answer ----
def should_continue(state: AgentState) -> Literal["execute", "answer"]:
    """看 plan 的 LLM 输出：有 tool_calls 就去执行，否则回答。"""
    last = state["messages"][-1]
    return "execute" if last.get("tool_calls") else "answer"


# ---- 组装图 ----
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute", execute_node)
    graph.add_node("answer", answer_node)

    graph.add_edge(START, "plan")
    # plan 后：条件边（动态路由）→ execute 或 answer
    graph.add_conditional_edges(
        "plan", should_continue, {"execute": "execute", "answer": "answer"}
    )
    # execute 后：回 plan 再决策（循环）
    graph.add_edge("execute", "plan")
    # answer 后：结束
    graph.add_edge("answer", END)

    # recursion_limit=10 → plan 最多跑 5 次（5 步循环）
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
async def run_agent(session_id: str, user_input: str) -> ChatResult:
    """
    带记忆对话的完整入口：读历史 → 跑 Agent → 写回记忆 → 返回回答 + 来源。

    D11 变化：返回值从 str 变成 ChatResult（answer + sources + invalid_citations）。
    这是破坏性改动，但调用方目前只有一个验证脚本，成本可控。

    端到端流程：
      ⓪ reset_sources()             重新绑定本请求的引用收集器（并发隔离的关键）
      ① get_window(session_id)      从 Redis 读最近 N 轮历史
      ② messages = 历史 + [本次提问]
      ③ agent.ainvoke(messages)     跑 LangGraph 的 ReAct 循环
          └─ 工具执行时把来源登记进收集器（结构通道）
      ④ 取最终回答（最后一条有内容的 assistant 消息）
      ⑤ get_sources()               取出本请求累计的来源
      ⑥ check_citations()           校验回答里的 [n] 有没有越界
      ⑦ append_turn(...)            写回 Redis（Upsert + 裁剪 + 刷新 TTL）
      ⑧ 返回 ChatResult

    Args:
        session_id: 会话 ID（前端生成的 UUID，一个会话固定一个）
        user_input: 用户本轮输入
    Returns:
        ChatResult：回答文本 + 来源映射表 + 越界引用编号
    """
    from app.services.memory_service import append_turn, get_window

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

    # ③ 跑图
    result = await get_agent().ainvoke(state)

    # ④ 取最终回答
    #   注意：中间会有带 tool_calls 的 assistant 消息（content 为空），
    #   所以要从后往前找"第一条 content 非空的 assistant 消息"
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

    # ⑦ 写回记忆（只存 user + 最终回答，不存中间工具消息——见 memory_service 说明）
    await append_turn(session_id, user_input, final_answer)

    # ⑧ 返回结构化结果
    logger.info(
        "Agent 完成 session=%s 回答长度=%d 来源数=%d 越界引用=%s",
        session_id,
        len(final_answer),
        len(sources),
        invalid or "无",
    )
    return ChatResult(answer=final_answer, sources=sources, invalid_citations=invalid)
