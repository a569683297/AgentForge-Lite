"""
请求级指标结果收集器（D37 / 块 3）
====================================
解决的问题：PRD F11.3 要求 `query_metric` "返回结构化 dict 而非字符串
（供前端图表直接消费）"，但**工具契约要求返回 str**（LLM 只认文本）——
与 D11 引用收集器（app/core/citation.py）是同一个矛盾的同一种解法：

    工具照常返回格式化文本（LLM 通道），**额外**把结构化结果登记到请求级收集器；
    run_agent 结束时取出，随 ChatResult / done 帧一起返回。
    → LLM 读文本写分析，前端拿结构画 ECharts，两者消费同一份事实。

与 citation.py 的关系：**同族但独立**（没合并成一个模块的理由）：
    · 引用来源（sources）服务于"回答里的 [n] 指哪篇文档"，粒度是片段；
    · 指标结果（metric_results）服务于"图表的数据集"，粒度是查询。
    生命周期相同（都按请求隔离、都在 run_agent 入口 reset），但形状、
    消费方、演进方向都不同——合并只会让两个无关形状挤一个文件。
    两者在 run_agent 里的取用位置刻意相邻，便于对照阅读。

⚠ 与 citation.py 同一条铁律（实测出来的，写之前必须知道）：
ContextVar 复制的是「绑定」不是「对象内容」。绑定动作必须发生在**每个请求
自己的 task 内部**（reset_metric_results 在 run_agent / run_agent_stream
入口调用）——这样每个请求各持一份 list，并发互不污染。
get 侧保留 default=None + 惰性绑定，保证"工具被单独调用（没走 run_agent）
或测试直接调用"时也不炸。
"""

import contextvars
from typing import Any

# default=None：没绑定过时取到 None → record 里惰性绑定一个新的（双保险）
_metric_results: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("metric_results", default=None)
)


def reset_metric_results() -> None:
    """
    重新绑定一个全新的收集器 —— **必须在每个请求入口调用**
    （run_agent / run_agent_stream 开头，与 reset_sources 并排）。

    为什么不能省：与 citation 完全同理——不重新绑定就会读到上一个请求
    残留的指标结果，或者与其他并发请求共享同一个 list（串数据）。
    """
    _metric_results.set([])


def record_metric_result(payload: dict[str, Any]) -> None:
    """
    登记一次指标查询的结构化结果（query_metric 工具在**成功返回后**调用）。

    只登记"查询成功"的结果（含 degraded 降级——它语义上是"结果:暂不可用"，
    前端需要渲染占位）。参数错误（MetricsError）**不登记**：
    那次没有产生任何数据，登记下来只会让前端画空图。

    ⚠ payload 必须已经是 JSON 安全值（rows 里的 date/Decimal 在
    metrics_service._jsonable 里已归一）——收集器不做转换，保持哑存储。
    """
    buf = _metric_results.get()
    if buf is None:  # 本 task 还没绑定（如工具被单独测试）→ 惰性绑定
        buf = []
        _metric_results.set(buf)
    buf.append(payload)


def get_metric_results() -> list[dict[str, Any]]:
    """取出当前请求收集到的全部指标结果（复制一份，避免调用方误改内部列表）。"""
    return list(_metric_results.get() or [])
