"""
可观测层入口（D28）
====================
Langfuse 埋点的**唯一出口** —— 与 `llm_gateway` 是「LLM 调用的唯一出口」同构。

三条设计理由，每条都对应一个实测过的坑：

1. **整个进程只能有一个 Langfuse 实例（这里复用它，绝不新建）**
   实例内部持有一个 OTEL TracerProvider + BatchSpanProcessor（后台批量导出）。
   建两个实例 = **两条独立的导出管道**：同一段工作被上报两次、后台线程与连接翻倍，
   而且两边**都看起来正常**（不报错、面板上就是有两份）。
   D5 已经在 `llm_gateway` 建了这个实例，所以这里 `from ... import langfuse`，
   而不是再 `Langfuse(...)` 一次。

2. **span 命名规范必须有唯一权威出处**
   PRD v4.1 §18 遗留问题 #5 点名「`latency_p95` 的 span 命名约定未统一」，
   并写明"依赖 D28 埋点时的 span name 规范"。与 `RETRIEVER_CONFIGS` /
   `LLM_CHANNELS` 同一个道理：**取值清单只能有一份**。
   否则 D35 的语义层按 name 分组聚合时，某处拼错只会**静默少一行**，不报错。
   ⚠ 这里比 PRD 的建议清单多了一个 `retrieval` 父 span（PRD 只列了
   embedding / vector_search / bm25 / rerank / generation）—— 见文件末尾说明。

3. **埋点绝不能阻断业务 —— 但这条不靠 try/except**
   实测 langfuse 4.15.1：`public_key` 为空时打印一行
   `Authentication error: ... Client will be disabled`，然后**照常返回一个
   no-op 的 span 对象**；`start_as_current_observation` 与 `propagate_attributes`
   **都不抛异常**。所以业务代码里**不加 try/except** ——
   加了反而会把"埋点静默失效"藏起来（那正是 D12 孤儿切片的教训：
   用异常兜住的失败，看起来和成功一样）。

埋点树的形状（D28 验收①「全链路可追踪」看的就是这个）：

    run_agent                  ← 根 span（as_type=agent），带 session_id
    ├─ agent-plan              ← generation（llm_gateway 建的，D5 起就有）
    ├─ tool:search_documents   ← tool，metadata.source = "local"
    │   └─ retrieval           ← 检索父（本模块新增）
    │       ├─ vector_search   ← retriever
    │       │   └─ embedding   ← embedding（generation-like，可带 model 名）
    │       ├─ bm25            ← retriever
    │       └─ rerank          ← retriever
    ├─ tool:harness_xxx        ← tool，metadata.source = "harness"  ★ 验收②
    └─ agent-answer            ← generation
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

# ⚠ 下面两个名字都带 "langfuse"，但**不是同一个东西**（本项目最容易混的一处）：
#     langfuse             ← **客户端实例**（llm_gateway 在 D5 建的，全进程唯一）
#     propagate_attributes ← **模块级函数**（不是实例方法）
#   实测：`hasattr(Langfuse, "propagate_attributes")` 为 **False** ——
#   写成 `langfuse.propagate_attributes(...)` 会 AttributeError，
#   而 `langfuse` 这个名字在 llm_gateway 里已经被实例占用了，很容易误导。
from langfuse import propagate_attributes

from app.services.llm_gateway import langfuse

# ============================================================
# span 命名规范（PRD §18 #5 要求 D28 定下来）
# ============================================================
# 这六个常量是**唯一权威出处**：埋点侧与 D35 的聚合侧都从这里取。
SPAN_ROOT = "run_agent"          # 根：一整轮对话
SPAN_RETRIEVAL = "retrieval"     # 检索父：一次检索的全部环节（见文件末尾说明）
SPAN_EMBEDDING = "embedding"     # 问题 → 向量
SPAN_VECTOR = "vector_search"    # pgvector 相似度检索
SPAN_BM25 = "bm25"               # 关键词（倒排 + BM25 自算）
SPAN_RERANK = "rerank"           # cross-encoder 精排


def tool_span_name(tool_name: str) -> str:
    """
    工具 span 的名字：`tool:<工具名>`（D28）。

    为什么带 `tool:` 前缀，而不是直接用工具名 —— 因为**工具名是用户可自定义的**：
    MCP server 想叫什么就叫什么，完全可能出现一个叫 `vector_search` 的工具。
    那时它和检索那一格在 Langfuse 上**同名**，`group by name` 会把两个东西混成一组，
    而 D35 的 `latency_p95` 正好是按 name 分组的 —— 混组不报错，只是那一行的数字错了。
    前缀把"工具层"和"检索层"在**名字层面**就分开。
    """
    return f"tool:{tool_name}"


@contextmanager
def span(name: str, as_type: str = "span", **kwargs: Any) -> Iterator[Any]:
    """
    创建一个挂在「当前活动 span」下的观测（observation）。

    两个"不用你操心"的机制（都实测过）：

    - **父是谁不用传**：OTEL 语义 —— 新 span 的父 = **当前上下文里正在活动的那个 span**。
      所以只要外层有 `with span(...)` 包着，父子关系自动成立。
      实测跨越两种"看起来会丢上下文"的场景仍然成立：
        ① 跨 LangGraph 节点（节点函数是独立协程）
        ② 跨 `asyncio.gather` 的并发分支
      原因是 OTEL 用 `contextvars` 实现，而 asyncio 在创建子任务时会**复制**当前上下文。
    - **end 是自动的**：退出 `with` 时 SDK 自动 `end()` 并记录 latency。
      所以 **with 块的范围 = 被计时的范围** —— 包太宽会把不相关的等待算进来。

    Args:
        name: span 名（用本模块的 SPAN_* 常量或 tool_span_name()，不要就地写字符串）
        as_type: 观察类型。取值见 langfuse 的 ObservationType；本项目用到的：
                 agent / generation / tool / retriever / embedding / chain。
                 ⚠ 类型决定"能带哪些字段"：generation 与 embedding 能带
                 model / usage_details / cost_details；**tool / retriever / span
                 带不了这些**（签名一样，但参数会被静默丢弃）。所以来源标识
                 只能写进 `metadata`。
        **kwargs: 原样透传给 `start_as_current_observation`，常用：
                  input / output / metadata / model / model_parameters / level / status_message
    """
    with langfuse.start_as_current_observation(
        name=name, as_type=as_type, **kwargs
    ) as obs:
        yield obs


@contextmanager
def trace_attrs(
    *,
    session_id: Any = None,
    user_id: Any = None,
    trace_name: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> Iterator[None]:
    """
    给「当前这条 trace」贴属性（会话 id / 用户 / trace 名）。

    为什么不用 `update_current_trace` —— 实测 langfuse 4.15.1 **没有这个方法**。
    网上大量教程在用它，那是 ≤3.x 的 API。4.x 的入口是模块级
    `langfuse.propagate_attributes`。

    ⚠ **顺序是硬约束**（实测踩过，官方 docstring 原文照抄）：
        "Pre-existing spans will NOT be retroactively updated"
        —— 已经建好的 span 不会被追溯更新。
      实测：把根 span 建在 `propagate_attributes` **之前**时，
      **根 span 的 session/user 是 `None`，而它的子 span 有值** ——
      因为属性是"进入上下文之后新建的 span"才带上。
      → 正确写法：`propagate_attributes` **包在根 span 外面**（或刚进根 span 时立刻调）。

    传 None 的项会被**跳过**（不下发）：`propagate_attributes` 把 None 当成
    "要设成 None"，而我们要的语义是"这项不管" —— 两者不是一回事。
    """
    attrs: dict[str, Any] = {}
    if session_id is not None:
        attrs["session_id"] = str(session_id)
    if user_id is not None:
        attrs["user_id"] = str(user_id)
    if trace_name is not None:
        attrs["trace_name"] = trace_name
    if metadata is not None:
        attrs["metadata"] = metadata
    if tags is not None:
        attrs["tags"] = tags

    if not attrs:
        # 一项都没有时不进上下文：多一层空上下文只会让代码看起来"设了什么"
        yield
        return

    with propagate_attributes(**attrs):
        yield


def current_trace_id() -> str | None:
    """
    当前上下文里的 trace id（十六进制字符串）；没有活动 trace 时返回 None。

    **给验证脚本取证用** —— 判断几层 span 是不是同一条 trace 靠它。
    （生产代码不需要读它：父子关系由上下文自动维护，不用手写 trace_id。）
    """
    return langfuse.get_current_trace_id()


# ============================================================
# 为什么多了一个 `retrieval` 父 span（超出 PRD 的建议清单）
# ============================================================
# PRD §18 #5 建议固定为 embedding / vector_search / bm25 / rerank / generation
# 五个名字。D28 额外加了一层 `retrieval`，理由是**降级归因**：
#
#   一次检索可能只走其中一部分（pure_vector 只有 vector_search；BM25 路可能
#   因为"查询全是标点"而无 token 直接返回空；rerank 可能超时降级）。
#   没有父节点时，"这一次检索一共花了多久"要靠在面板上手动把几条拉在一起看；
#   有了 `retrieval` 一格，它的 latency **就是这一次检索的总耗时**，
#   而且它的 `output` 里记着实际走了哪几步 —— D35 的 `latency_p95`
#   既能按 name 看单段，也能直接读这一格看整体。
#
# ⚠ 它不是"多埋一层"：`retrieval` 的耗时**包含**子 span 的耗时（嵌套计时），
#   所以做"各段耗时占比"时**不能把它和子段相加** —— 那是重复计算。
