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
            result = execute_tool(fn_name, args)
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
    """带着工具结果生成最终回答。"""
    messages = state["messages"]
    system = (
        "你是 AI 助手。如果上面有工具执行结果，请基于结果回答用户；"
        "否则直接回答。不要提及内部工具调用细节。"
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
# 带记忆的 Agent 入口（D8 新增）
# ============================================================
async def run_agent(session_id: str, user_input: str) -> str:
    """
    带记忆对话的完整入口：读历史 → 跑 Agent → 写回记忆 → 返回回答。

    端到端流程：
      ① get_window(session_id)      从 Redis 读最近 N 轮历史
      ② messages = 历史 + [本次提问]
      ③ agent.ainvoke(messages)     跑 LangGraph 的 ReAct 循环
      ④ 取最终回答（最后一条有内容的 assistant 消息）
      ⑤ append_turn(...)            写回 Redis（Upsert + 裁剪 + 刷新 TTL）
      ⑥ 返回回答

    Args:
        session_id: 会话 ID（前端生成的 UUID，一个会话固定一个）
        user_input: 用户本轮输入
    Returns:
        Agent 的回答文本
    """
    from app.services.memory_service import append_turn, get_window

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

    # ⑤ 写回记忆（只存 user + 最终回答，不存中间工具消息——见 memory_service 说明）
    await append_turn(session_id, user_input, final_answer)

    return final_answer
