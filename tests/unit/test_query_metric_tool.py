"""
单元：query_metric 工具（注册 / 动态描述 / 格式化 / 防护 / 收集器）
====================================================================
这层测什么、不测什么：
  · **测**工具自身的四个出参分支（成功 / 空结果 / 降级 / 参数被拒）、
    动态描述生成、结构登记（收集器）语义、以及"注入永远到不了数据库"的
    **否定性承诺**（mock 掉执行点做间谍，断言它没被调用）。
  · **不测**真库/真 Langfuse —— 那是 integration/test_query_metric_tool.py
    的职责。这里连 _fetch_sql 都 mock 掉，跑完不碰任何 IO。

一条要钉死的**契约**（不是普通断言）：错误文本必须含「执行失败」四个字。
agent_service._is_tool_failure() 与评测口径都用它判定工具失败；
改文案忘改判定 = "参数被拒"在观测层看起来像成功（静默污染排障与评测）。
"""

import pytest

from app.core import metric_results as mr
from app.services import metrics_service as ms
from app.tools import analytics
from app.tools.registry import execute_tool, get_tool_source


@pytest.fixture(autouse=True)
def _clean_collector():
    """每个用例前重置收集器 —— 保证用例之间互不串数据（等价于请求入口的 reset）。"""
    mr.reset_metric_results()
    yield


def _fake_run_metric(payload: dict):
    """伪造 run_metric 的返回值：形状与 metrics_service.run_metric 的出口一致。"""

    async def _fake(metric, dimension=None, start="", end=""):
        return payload

    return _fake


# ============================================================
# 一、注册与动态描述（口径唯一来源：metrics.yaml）
# ============================================================
def test_tool_registered_as_local():
    """query_metric 在注册表里，且 source=local（进程内的真函数，非 MCP 代理）。"""
    from app.tools import list_tools

    assert "query_metric" in list_tools()
    assert get_tool_source("query_metric") == "local"


def test_description_lists_every_registered_metric():
    """
    工具描述必须动态包含**全部**已注册指标（名字 + 可用维度）。

    这条测的是"动态生成机制在工作"：描述是模块加载时从 METRIC_REGISTRY 拼的，
    YAML 加指标 → 工具描述自动同步。若哪天有人把它改回手写清单，
    这条会在加/改指标时立刻变红（而不是等 LLM 按过期清单调用才暴露）。
    """
    from app.tools import get_tools_schema

    schema = next(
        s for s in get_tools_schema() if s["function"]["name"] == "query_metric"
    )
    desc = schema["function"]["description"]
    for name, spec in ms.METRIC_REGISTRY.items():
        assert name in desc, f"指标 {name} 没进工具描述"
        if spec.dimensions:
            for dim in spec.dimensions:
                assert dim in desc, f"指标 {name} 的维度 {dim} 没进工具描述"
    assert "current_time" in desc, "描述里应提示先查当前日期"


def test_parameters_schema_shape():
    """参数 schema：4 个属性；required = metric/start_date/end_date（dimension 可选）。"""
    from app.tools import get_tools_schema

    schema = next(
        s for s in get_tools_schema() if s["function"]["name"] == "query_metric"
    )
    params = schema["function"]["parameters"]
    assert set(params["properties"]) == {"metric", "dimension", "start_date", "end_date"}
    assert set(params["required"]) == {"metric", "start_date", "end_date"}


# ============================================================
# 二、成功路径：文本通道 + 结构通道
# ============================================================
async def test_success_formats_rows_and_records_structure(monkeypatch):
    """成功：文本含口径头部与逐行数据；收集器登记一份完整结构。"""
    monkeypatch.setattr(
        analytics,
        "run_metric",
        _fake_run_metric(
            {
                "metric": "eval_accuracy",
                "dimension": "config_name",
                "source": "sql",
                "rows": [
                    {"dim": "hybrid_rerank", "value": 0.965, "runs": 4},
                    {"dim": "hybrid", "value": 0.96, "runs": 3},
                ],
                "degraded": False,
                "error": None,
            }
        ),
    )

    text = await analytics.query_metric(
        "eval_accuracy", "config_name", "2026-10-01", "2026-10-16"
    )

    # 文本通道：口径头部（LLM 写分析时要引用）+ 逐行数据
    assert "评测准确率" in text and "eval_accuracy" in text
    assert "config_name" in text
    assert "2026-10-01 ~ 2026-10-16" in text
    assert "hybrid_rerank" in text and "0.965" in text
    assert "共 2 行" in text
    assert "执行失败" not in text

    # 结构通道：一条完整登记（前端画图所需字段齐备）
    results = mr.get_metric_results()
    assert len(results) == 1
    item = results[0]
    assert item["metric"] == "eval_accuracy"
    assert item["label"] == "评测准确率"
    assert item["dimension"] == "config_name"
    assert item["source"] == "sql"
    assert item["start_date"] == "2026-10-01"
    assert item["end_date"] == "2026-10-16"
    assert item["degraded"] is False
    assert len(item["rows"]) == 2


async def test_aggregate_metric_without_dimension(monkeypatch):
    """聚合指标（无维度）：文本标"无（聚合值）"，结构里 dimension=None。"""
    monkeypatch.setattr(
        analytics,
        "run_metric",
        _fake_run_metric(
            {
                "metric": "session_count",
                "dimension": None,
                "source": "sql",
                "rows": [{"value": 189}],
                "degraded": False,
                "error": None,
            }
        ),
    )

    text = await analytics.query_metric("session_count", None, "2026-09-01", "2026-10-16")

    assert "无（聚合值）" in text
    assert "189" in text
    assert mr.get_metric_results()[0]["dimension"] is None


async def test_empty_rows_guards_against_fabrication(monkeypatch):
    """空结果：明确说明 + 防幻觉提示（"不要编造"），且仍登记（前端画空态而非缺块）。"""
    monkeypatch.setattr(
        analytics,
        "run_metric",
        _fake_run_metric(
            {
                "metric": "message_volume",
                "dimension": "day",
                "source": "sql",
                "rows": [],
                "degraded": False,
                "error": None,
            }
        ),
    )

    text = await analytics.query_metric("message_volume", "day", "2020-01-01", "2020-01-02")

    assert "查询结果为空" in text
    assert "不要编造" in text
    assert len(mr.get_metric_results()) == 1  # 登记了：前端知道"查过，是空的"


async def test_degraded_langfuse_renders_unavailable(monkeypatch):
    """降级（Langfuse 不可达）：文本给"暂不可用 + 不要编造"，结构 degraded=True。"""
    monkeypatch.setattr(
        analytics,
        "run_metric",
        _fake_run_metric(
            {
                "metric": "token_cost",
                "dimension": "model",
                "source": "langfuse_api",
                "rows": [],
                "degraded": True,
                "error": "Langfuse 不可达：连接超时",
            }
        ),
    )

    text = await analytics.query_metric("token_cost", "model", "2026-10-15", "2026-10-16")

    assert "暂时不可用" in text
    assert "不要编造" in text
    assert "连接超时" in text
    item = mr.get_metric_results()[0]
    assert item["degraded"] is True
    assert item["error"] == "Langfuse 不可达：连接超时"


# ============================================================
# 三、参数被拒：纠错文本（不抛异常）+ 不登记
# ============================================================
async def test_unknown_metric_returns_actionable_error():
    """未知指标：真实 run_metric 拒绝 → 文本含"执行失败"+可用指标清单；不登记。"""
    text = await analytics.query_metric("nonexistent_metric", None, "2026-10-01", "2026-10-02")

    assert "query_metric 执行失败" in text
    assert "未知指标" in text
    assert "eval_accuracy" in text  # 错误消息列出可用取值（给 LLM 的纠错提示）
    assert mr.get_metric_results() == []


async def test_injection_dimension_never_reaches_db(monkeypatch):
    """
    注入维度：真 run_metric 在 render_sql 拒绝 → 
    **执行点 _fetch_sql 从未被调用**（否定性承诺的硬证明）+ 不登记。
    """
    calls: list[str] = []

    async def spy_fetch_sql(sql, params):
        calls.append(sql)
        return []

    monkeypatch.setattr(ms, "_fetch_sql", spy_fetch_sql)

    text = await analytics.query_metric(
        "tool_usage", "tool_name; DROP TABLE messages; --", "2026-10-01", "2026-10-02"
    )

    assert "执行失败" in text
    assert "不支持的维度" in text
    assert calls == [], "注入串居然到达了执行点"
    assert mr.get_metric_results() == []


async def test_bad_date_format_rejected():
    """日期非法：拒绝并指出正确格式；不登记。"""
    text = await analytics.query_metric("session_count", None, "2026/10/01", "2026-10-02")

    assert "执行失败" in text
    assert "YYYY-MM-DD" in text
    assert mr.get_metric_results() == []


async def test_aggregate_metric_with_dimension_rejected():
    """聚合指标传了维度：拒绝并说明"不支持分组"，不静默忽略。"""
    text = await analytics.query_metric("session_count", "config_name", "2026-10-01", "2026-10-02")

    assert "执行失败" in text
    assert "不支持分组" in text
    assert mr.get_metric_results() == []


# ============================================================
# 四、契约守卫（防将来改文案时静默破坏观测/评测）
# ============================================================
def test_failure_text_contract_documented():
    """
    守卫：失败判定依赖「执行失败」子串（agent_service._is_tool_failure）。

    这里做的是**源码级探查**（读 analytics.py 文本）：确认错误分支的
    返回语句里确实带着这四个字 —— 比运行时构造场景更直接地钉在"代码写了什么"上。
    若将来有人改文案（比如换成"查不到"），这条会红，提醒先看模块头的契约说明。
    """
    import inspect

    src = inspect.getsource(analytics.query_metric)
    assert "执行失败" in src, (
        "query_metric 的错误文案丢了「执行失败」标记 —— "
        "它是 _is_tool_failure 的判定依据，改文案前先改判定（或看模块头说明）"
    )


# ============================================================
# 五、收集器语义（与 citation 同族）
# ============================================================
async def test_record_order_and_reset_isolation(monkeypatch):
    """两次查询按调用顺序保序；reset 后清空（请求隔离语义）。"""
    monkeypatch.setattr(
        analytics,
        "run_metric",
        _fake_run_metric(
            {
                "metric": "session_count",
                "dimension": None,
                "source": "sql",
                "rows": [{"value": 1}],
                "degraded": False,
                "error": None,
            }
        ),
    )

    await analytics.query_metric("session_count", None, "2026-10-01", "2026-10-02")
    await analytics.query_metric("session_count", None, "2026-10-01", "2026-10-02")
    assert len(mr.get_metric_results()) == 2

    mr.reset_metric_results()
    assert mr.get_metric_results() == []


# ============================================================
# 六、经注册表调用（execute_tool 端到端：awaitable 探测 + str 契约）
# ============================================================
async def test_execute_tool_end_to_end_with_mocked_db(monkeypatch):
    """
    走 execute_tool（与 Agent 真实调用同一条路）：
      · 异步工具的 awaitable 探测正常工作（返回文本而非 coroutine repr）
      · SQL 是**模板渲染**的结果（不是自由生成）——断言渲染出的 SQL 含模板特征
      · 时间参数是**绑定字典**（:start / :end 对应值），不是字符串拼接
    """
    captured: dict = {}

    async def fake_fetch_sql(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"value": 189}]

    monkeypatch.setattr(ms, "_fetch_sql", fake_fetch_sql)

    output = await execute_tool(
        "query_metric",
        {"metric": "session_count", "start_date": "2026-09-01", "end_date": "2026-10-16"},
    )

    assert "189" in output
    assert "coroutine" not in output, "execute_tool 没把 coroutine await 掉"
    # SQL 来自 metrics.yaml 模板（含固定的表名与时间列），而不是模型拼的
    assert "FROM sessions" in captured["sql"]
    assert "created_at >= :start" in captured["sql"]
    # 时间走绑定：参数是带时区的 datetime 对象
    assert captured["params"]["start"].tzinfo is not None
    assert captured["params"]["end"].tzinfo is not None
    # 结构通道同步生效
    assert mr.get_metric_results()[0]["rows"] == [{"value": 189}]
