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
from app.tools.current_time import current_time

# ---- 工具表：name → (函数, 描述, 参数schema) ----
# D9 升级为 ToolRegistry，这里先内联
TOOLS = {
    "current_time": {
        "function": current_time,
        "description": "获取当前日期和时间。当用户问'现在几点/今天几号/当前时间'时使用。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }
}


def _build_tools_schema() -> list[dict]:
    """把 TOOLS 转成 OpenAI function calling 的 tools schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": meta["description"],
                "parameters": meta["parameters"],
            },
        }
        for name, meta in TOOLS.items()
    ]


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
        _build_tools_schema(),
        trace_name="agent-plan",
    )
    # LLM 返回的完整 assistant 消息（可能含 tool_calls 数组）
    assistant_msg = result["message"]
    return {"messages": messages + [assistant_msg]}


# ---- 节点 2：execute（执行工具）+ observe（结果写回）----
async def execute_node(state: AgentState) -> AgentState:
    """执行 assistant 消息里声明的 tool_calls，结果以 tool 消息写回。"""
    messages = state["messages"]
    last = messages[-1]  # plan 追加的 assistant 消息（含 tool_calls）
    tool_calls = last.get("tool_calls") or []

    tool_messages = []
    for tc in tool_calls:
        try:
            import json

            fn_name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            result = TOOLS[fn_name]["function"](**args)
        except Exception as e:
            result = f"工具执行失败: {e}"
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
