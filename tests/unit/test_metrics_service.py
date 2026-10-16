"""
单元：语义层纯逻辑（注册表加载 / 模板渲染 / 时间窗解析 / 注入防护）
====================================================================
为什么不碰数据库：本模块的全部"判断"（白名单、占位符、日期校验）都发生在
SQL 执行**之前** —— 能在这里测掉的，就不留给集成测试。注入类用例尤其如此：
它验证的是一条**否定性承诺**——"这些字符串根本到不了数据库"，
否定性承诺只有把"拒绝发生在入口"钉死才算数（而不是"侥幸没执行成功"）。

纪律（与全项目一致）：
· 不写库、不碰网络 —— 执行路径的验证在 integration/test_metrics_api.py；
· 断言按"用户能不能据此行动"打 —— 错误消息必须**列出可用取值**
  （它是给 LLM 的纠错提示，含糊的错误 = 模型原地打转）。
"""

import pytest

from app.services import metrics_service as ms

# ============================================================
# 一、注册表加载：结构完整 + 首批指标在册
# ============================================================
EXPECTED_METRICS = {
    "session_count",
    "message_count",
    "message_volume",
    "tool_usage",
    "eval_accuracy",
    "eval_dimension_scores",
    "eval_failures_by_category",
    # 块 2：Langfuse 源
    "latency_p95",
    "token_cost",
}


def test_registry_loads_expected_metrics():
    """首批 SQL 源指标全部在册（metrics.yaml 是唯一出处，这里钉住名单防误删）。"""
    assert EXPECTED_METRICS <= set(ms.METRIC_REGISTRY)


def test_all_specs_have_core_fields():
    """每个指标：中文名/说明非空；source 合法；sql 源有模板，langfuse 源有端点与查询。"""
    for name, spec in ms.METRIC_REGISTRY.items():
        assert spec.label and spec.description, name
        assert spec.source in ms.VALID_SOURCES, name
        if spec.source == "sql":
            assert spec.sql_template, name
        else:
            assert spec.endpoint and spec.query, name


def test_dimensioned_metrics_use_placeholder():
    """声明了维度的 **sql 源** 指标，模板里必须真有 {dimension}（加载期已校验，这里防回归）。

    langfuse 源没有 sql_template，维度通过 query.group_by 表达 —— 不在本用例范围
    （它的对应校验是 test_load_rejects_bad_yaml 的 ⑧）。
    """
    for name, spec in ms.METRIC_REGISTRY.items():
        if spec.source == "sql" and spec.dimensions:
            assert "{dimension}" in (spec.sql_template or ""), name


def test_load_rejects_bad_yaml():
    """
    加载期校验的反面用例：四种坏 YAML 都必须被拒（写错就启动失败，不留到运行期）。

    用 tempfile.mkdtemp 而不是 pytest 的 tmp_path：本项目的验证要在多种执行环境
    下可重复 —— 某些带文件系统钩子的运行环境里，"已存在目录再 mkdir" 会被
    包装成 PermissionError（正常环境是 FileExistsError），而 pytest 的 tmp_path
    在目录已存在时正要走这条路径。mkdtemp 每次都是全新唯一目录，不碰这个坑。
    """
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="af-metrics-bad-"))

    def write_and_load(body: str):
        path = tmpdir / "bad.yaml"
        path.write_text(body, encoding="utf-8")
        return ms._load_registry(path)

    try:
        # ① 未知 source
        with pytest.raises(ms.MetricsError, match="source 非法"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: mysql\n    dimensions: []\n"
                "    sql_template: SELECT 1 AS value\n"
            )

        # ② 模板含非法占位符（如把绑定参数误写成 {start}）
        with pytest.raises(ms.MetricsError, match="非法占位符"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: sql\n    dimensions: []\n"
                "    sql_template: SELECT 1 AS value WHERE created_at >= {start}\n"
            )

        # ③ 声明了维度但模板没用 {dimension}
        with pytest.raises(ms.MetricsError, match="没有 \\{dimension\\}"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: sql\n    dimensions: [day]\n"
                "    sql_template: SELECT COUNT(*) AS value FROM t\n"
            )

        # ④ 没声明维度却在模板里用了 {dimension}
        with pytest.raises(ms.MetricsError, match="未声明维度"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: sql\n    dimensions: []\n"
                "    sql_template: SELECT {dimension} AS dim, COUNT(*) AS value FROM t GROUP BY {dimension}\n"
            )

        # ⑤ langfuse 源缺 endpoint
        with pytest.raises(ms.MetricsError, match="endpoint 为空"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: langfuse_api\n    dimensions: []\n"
                "    query: {agg: sum, field: latency}\n"
            )

        # ⑥ langfuse 源的 agg 不在取数层白名单（写错 = 运行时才炸）
        with pytest.raises(ms.MetricsError, match="query.agg 非法"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: langfuse_api\n    dimensions: []\n"
                "    endpoint: /api/public/observations\n"
                "    query: {agg: median, field: latency}\n"
            )

        # ⑦ langfuse 源的 field 不在取数层白名单
        with pytest.raises(ms.MetricsError, match="query.field 非法"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: langfuse_api\n    dimensions: []\n"
                "    endpoint: /api/public/observations\n"
                "    query: {agg: sum, field: temperature}\n"
            )

        # ⑧ group_by 不在该指标的 dimensions 白名单里（分组列与声明不一致）
        with pytest.raises(ms.MetricsError, match="group_by"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: langfuse_api\n    dimensions: [name]\n"
                "    endpoint: /api/public/observations\n"
                "    query: {agg: p95, field: latency, group_by: model}\n"
            )

        # ⑨ 互斥配置段（复制粘贴 YAML 改 source 不删配置的典型误写）
        with pytest.raises(ms.MetricsError, match="却带着 query 段"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: sql\n    dimensions: []\n"
                "    sql_template: SELECT 1 AS value\n"
                "    query: {agg: sum, field: latency}\n"
            )
        with pytest.raises(ms.MetricsError, match="却带着 sql_template"):
            write_and_load(
                "metrics:\n  m:\n    label: x\n    source: langfuse_api\n    dimensions: []\n"
                "    endpoint: /api/public/observations\n"
                "    sql_template: SELECT 1 AS value\n"
                "    query: {agg: sum, field: latency}\n"
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# 二、防护②：维度白名单（含注入样例）
# ============================================================
def test_render_substitutes_whitelisted_dimension():
    spec = ms.METRIC_REGISTRY["tool_usage"]
    sql = ms.render_sql(spec, "tool_name")
    assert "tool_name" in sql
    assert "{dimension}" not in sql          # 占位符必须全部替换掉


def test_render_rejects_dimension_not_in_whitelist():
    spec = ms.METRIC_REGISTRY["tool_usage"]
    with pytest.raises(ms.MetricsError, match="不支持的维度") as exc:
        ms.render_sql(spec, "passed")
    # 错误消息必须列出可用维度 —— 它同时是给 LLM 的纠错提示
    assert "tool_name" in str(exc.value)


@pytest.mark.parametrize(
    "evil",
    [
        "tool_name; DROP TABLE messages; --",
        "tool_name' OR '1'='1",
        "(SELECT 1) AS x",
        "name UNION SELECT password FROM users",
    ],
)
def test_render_sql_injection_attempts_all_rejected(evil):
    """注入串的共同命运：不在白名单 → 拒绝 → **到不了数据库**。"""
    spec = ms.METRIC_REGISTRY["tool_usage"]
    with pytest.raises(ms.MetricsError):
        ms.render_sql(spec, evil)


def test_render_dimensioned_metric_requires_dimension():
    """有维度的指标不传维度 → 拒绝（而不是按第一个维度静默兜底）。"""
    spec = ms.METRIC_REGISTRY["message_volume"]
    with pytest.raises(ms.MetricsError, match="不支持的维度"):
        ms.render_sql(spec, None)


def test_render_aggregate_metric_rejects_dimension():
    """聚合指标传了维度 → 拒绝并说明"该指标不支持分组"。"""
    spec = ms.METRIC_REGISTRY["session_count"]
    with pytest.raises(ms.MetricsError, match="聚合指标"):
        ms.render_sql(spec, "day")


def test_render_aggregate_metric_ok_without_dimension():
    spec = ms.METRIC_REGISTRY["session_count"]
    sql = ms.render_sql(spec, None)
    assert "COUNT(*)" in sql


# ============================================================
# 三、防护③：时间窗解析（北京时间边界 + 闭区间语义）
# ============================================================
def test_parse_window_converts_to_beijing_boundaries():
    """[2026-09-01, 2026-10-16] → 左闭右开、北京时间，右界是 end 的次日 00:00。"""
    start, end = ms.parse_window("2026-09-01", "2026-10-16")
    assert start.isoformat() == "2026-09-01T00:00:00+08:00"
    assert end.isoformat() == "2026-10-17T00:00:00+08:00"


def test_parse_window_single_day():
    """start == end 是合法的单日窗口。"""
    start, end = ms.parse_window("2026-10-16", "2026-10-16")
    assert start.isoformat() == "2026-10-16T00:00:00+08:00"
    assert end.isoformat() == "2026-10-17T00:00:00+08:00"


@pytest.mark.parametrize(
    "start, end",
    [
        ("2026/09/01", "2026-10-16"),   # 分隔符错
        ("20260901", "2026-10-16"),     # 无分隔（Python 3.11 起 fromisoformat 本可解析，被正则拦下）
        ("2026-13-01", "2026-10-16"),   # 月份不存在
        ("2026-02-30", "2026-03-01"),   # 日期不存在
        ("", "2026-10-16"),             # 空串
        ("2026-10-17", "2026-10-16"),   # start 晚于 end
    ],
)
def test_parse_window_rejects_bad_input(start, end):
    with pytest.raises(ms.MetricsError):
        ms.parse_window(start, end)


# ============================================================
# 四、防护①：指标名白名单 + 清单出口
# ============================================================
def test_get_spec_unknown_lists_available():
    with pytest.raises(ms.MetricsError, match="未知指标") as exc:
        ms.get_spec("profit_margin")
    # 消息列出全部可用指标（名字 + 中文名）
    assert "eval_accuracy" in str(exc.value)
    assert "评测准确率" in str(exc.value)


def test_list_metrics_shape():
    items = ms.list_metrics()
    assert {item["name"] for item in items} >= EXPECTED_METRICS
    assert all({"name", "label", "description", "source", "dimensions"} <= set(i) for i in items)


def test_jsonable_normalizes_types():
    """行归一化：date/datetime → ISO 串，Decimal → float（JSON 序列化安全）。"""
    import datetime as dt
    from decimal import Decimal

    assert ms._jsonable(dt.date(2026, 10, 16)) == "2026-10-16"
    assert ms._jsonable(Decimal("0.96")) == 0.96
    assert ms._jsonable(58) == 58
    assert ms._jsonable(None) is None
