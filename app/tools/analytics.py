"""
工具：query_metric（D37 / PRD §9.10 · F11 —— Agentic BI 入口）
================================================================
把语义层（metrics_service.run_metric）包装成 Agent 可调用的工具。
LLM 不写 SQL —— 它只选**三样**：指标名、维度、时间窗；SQL 由 metrics.yaml
模板渲染、维度必须命中白名单、时间走参数绑定（三层防护见 metrics_service.py）。

这个工具**不实现任何防护**——防护全部在 run_metric 里，本文件只负责：
    ① 把 LLM 的参数原样交给 run_metric（不绕过、不预处理）
    ② 成功 → 格式化文本（LLM 通道）+ 登记结构（前端通道，F11.3）
    ③ 参数被拒 → 把纠错信息交回 LLM（不是抛异常）——"执行失败"四个字是刻意的，见下

⚠ 错误文本为什么必须以「query_metric 执行失败」开头（不能随意改文案）：
    agent_service._is_tool_failure() 用 `"执行失败" in output` 判定工具失败——
    这个判定被**两处共用**：工具 span 的 ERROR 状态 + 流式 end 事件的 ok 字段；
    评测侧也据此对齐（工具调错 → 卡片标红 / trace 可检索）。
    改掉这四个字会让"参数被拒"在观测层看起来像成功——静默污染排障与评测。
    （tests/unit/test_query_metric_tool.py 有守卫测试钉住这个契约。）

description 是**动态生成**的（_metric_catalog 从 METRIC_REGISTRY 拼）：
口径的唯一来源永远是 metrics.yaml —— 若把指标清单手写死在这里，
改了 YAML 忘改文案，就会出现"LLM 看到的清单 ≠ 实际可用指标"的静默分裂。
"""

from typing import Any

from app.core.metric_results import record_metric_result
from app.services.metrics_service import (
    METRIC_REGISTRY,
    MetricSpec,
    MetricsError,
    run_metric,
)
from app.tools.registry import register

# 数据源的展示名（给人/LLM 读的中文口径；机器值仍是 sql / langfuse_api）
_SOURCE_LABELS = {
    "sql": "PostgreSQL（项目库）",
    "langfuse_api": "Langfuse（运行追踪）",
}


def _metric_catalog() -> str:
    """从 METRIC_REGISTRY 动态生成"可用指标"清单（工具 description 的一部分）。

    空白归一（`" ".join(desc.split())`）：YAML 块标量里的换行/缩进折叠进
    单行描述后会留下连续空格，给 LLM 的文本保持干净整洁。
    """
    lines: list[str] = []
    for name, spec in METRIC_REGISTRY.items():
        desc = " ".join(spec.description.split())
        dims = "、".join(spec.dimensions) if spec.dimensions else "无（聚合指标，不可分组）"
        lines.append(f"- {name}：{spec.label}。{desc}；可用维度：{dims}")
    return "\n".join(lines)


@register(
    name="query_metric",
    description=(
        "按口径查询系统运行指标（Agentic BI）。"
        "当用户询问系统的运行数据——评测准确率、Token 消耗、各阶段延迟、"
        "工具调用情况、对话量/消息量——时使用。"
        "不要用于闲聊、知识库文档问题或与系统运行数据无关的问题。\n"
        "只能按「指标名 + 维度 + 时间窗」查询，不支持自定义查询条件。\n"
        "时间范围是必填参数（YYYY-MM-DD，北京时间自然日，含起止两天）。"
        "若用户用相对时间表达（今天/最近三天/本周/上个月/最近一周），"
        "**必须先调用 current_time 工具确认当前日期**，再换算成具体日期 ——"
        "不要凭记忆假设年份（实测教训：模型曾把「最近一个月」写成去年的日期，"
        "查出一片空数据）。\n"
        "可用指标（metric 必须从下面选一个，不允许自造）：\n"
        + _metric_catalog()
    ),
    parameters={
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "description": "指标名，必须从工具描述列出的清单中选择",
            },
            "dimension": {
                "type": "string",
                "description": (
                    "分组维度（可选），必须是该指标声明的可用维度之一；"
                    "不传 = 聚合/不分组（聚合指标只能不传）"
                ),
            },
            "start_date": {
                "type": "string",
                "description": "起始日期 YYYY-MM-DD（含当天，北京时间）",
            },
            "end_date": {
                "type": "string",
                "description": "结束日期 YYYY-MM-DD（含当天，北京时间）",
            },
        },
        "required": ["metric", "start_date", "end_date"],
    },
)
async def query_metric(
    metric: str,
    dimension: str | None = None,
    start_date: str = "",
    end_date: str = "",
) -> str:
    """
    查询一个系统运行指标，返回给 LLM 读的格式化文本。

    三个出参分支（对照 run_metric 的契约）：
      · 参数被拒（MetricsError）—— 返回"执行失败 + 原因 + 正确用法"，
        **不登记**结构（这次没有产生数据）。错误消息本身即 LLM 的纠错提示。
      · 降级（langfuse 源不可达）—— 返回"暂不可用"说明 + 明确指令
        "不要编造数值"（防幻觉关键句），**登记**结构（degraded=True，
        前端渲染占位而不是画空图）。
      · 成功 —— 格式化行数据（LLM 通道）+ 登记结构（前端通道）。
    """
    try:
        result = await run_metric(metric, dimension, start_date, end_date)
    except MetricsError as exc:
        # 直接返回而不是 raise：execute_tool 会把异常吞成"执行失败：{e}"，
        # 但那样 LLM 拿到的信息被裁剪过（少了"该怎么改"）；这里给完整纠错。
        return (
            f"query_metric 执行失败：{exc}\n"
            "请检查参数（指标名 / 维度 / 日期格式）后重试。"
        )

    spec = METRIC_REGISTRY[result["metric"]]

    # ① 结构通道：登记给前端（F11.3 的"返回结构化 dict"落地——见模块头说明）。
    #    带时间窗：前端画图要标注"这个数据集对应哪段时间"。
    record_metric_result(
        {
            "metric": result["metric"],
            "label": spec.label,
            "dimension": result["dimension"],
            "source": result["source"],
            "start_date": start_date,
            "end_date": end_date,
            "degraded": result["degraded"],
            "error": result["error"],
            "rows": result["rows"],
        }
    )

    # ② 文本通道：格式化给 LLM
    return _format_result(result, spec, start_date, end_date)


def _format_result(
    result: dict[str, Any], spec: MetricSpec, start_date: str, end_date: str
) -> str:
    """
    把统一结构的查询结果格式化成 LLM 易读的文本。

    为什么逐行写「列名=值」而不是渲染 markdown 表格：
    各指标的数值列不统一（value / runs / scored_runs / correctness / …），
    写死表头会与 YAML 演化脱钩；「列名=值」由行数据自描述，加列不改代码。
    """
    header = (
        f"指标：{spec.label}（{spec.name}）\n"
        f"维度：{result['dimension'] or '无（聚合值）'} ｜ "
        f"时间窗：{start_date} ~ {end_date}（北京时间） ｜ "
        f"数据源：{_SOURCE_LABELS.get(result['source'], result['source'])}"
    )

    if result["degraded"]:
        # 防幻觉关键句：不给数据时**显式禁止编造**——LLM 面对"空结果"
        # 最危险的行为就是"补一个看起来合理的数字"。
        return (
            f"{header}\n"
            f"⚠ 该指标暂时不可用（数据源不可达）：{result['error']}\n"
            "请转告用户该数据暂无法获取并说明原因；**不要编造任何数值**。"
        )

    rows = result["rows"]
    if not rows:
        return (
            f"{header}\n"
            "查询结果为空：该时间窗内没有数据。"
            "请向用户说明，并确认时间范围是否符合他的意图（不要编造数值）。"
        )

    lines = [header, f"共 {len(rows)} 行："]
    for row in rows:
        # dim 列（若有）放最前，其余数值列按原顺序
        parts = [f"{k}={v}" for k, v in row.items()]
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)
