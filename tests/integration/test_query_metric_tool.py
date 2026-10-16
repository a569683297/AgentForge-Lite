"""
集成：query_metric 工具（真打注册表 + 真实 Postgres / Langfuse，**只读**）
============================================================================
与 unit/test_query_metric_tool.py 的分工：单元层把执行点 mock 掉测"分支与承诺"，
这里**不 mock** —— 走 execute_tool → analytics → metrics_service → 真数据源
的完整链路，证明工具层接上真实世界后契约依然成立。

断言口径与 test_metrics_api.py 一致：钉"结构 + 稳定常量 + 跨层一致"，
不钉精确数值（数值随演示数据增长，"假报警"的老路）。

四个用例各钉一条链上事实：
  1. 真库出数：双通道（文本 ↔ 结构）来自同一份结果的**同源性**；
  2. Langfuse 源：真数据或降级二选一（限流是常态，两种情况都必须干净）；
  3. 注入经注册表：拒绝 + 无登记 + 后续正常查询证明库无损；
  4. 跨层一致：工具链与 API 层同窗口查同一指标，数值逐位相同
     （两条入径同一出口 —— 防护与口径不会因为"走哪条路"而分叉）。
"""

import pytest

from app.core import metric_results as mr
from app.tools import execute_tool

pytestmark = pytest.mark.db

WIDE = {"start_date": "2020-01-01", "end_date": "2030-01-01"}


@pytest.fixture(autouse=True)
def _clean_collector():
    """每个用例前重置收集器（等价于 run_agent 入口的 reset_metric_results）。"""
    mr.reset_metric_results()
    yield


# ============================================================
# 一、真库出数：双通道同源
# ============================================================
async def test_sql_source_returns_real_data_via_registry():
    """会话总数经完整链路出真数；文本里的数值与结构里的数值一致（同源）。"""
    output = await execute_tool("query_metric", {"metric": "session_count", **WIDE})

    assert "执行失败" not in output
    assert "会话总数" in output

    results = mr.get_metric_results()
    assert len(results) == 1
    value = results[0]["rows"][0]["value"]
    assert value > 0
    # 文本通道与结构通道读的是同一份结果（不是两次查询）
    assert str(value) in output


async def test_grouped_metric_returns_rows_and_dim():
    """分组指标：行数 ≥1 且每行带 dim 列（前端绘图的列契约）。"""
    output = await execute_tool(
        "query_metric", {"metric": "tool_usage", "dimension": "tool_name", **WIDE}
    )

    assert "执行失败" not in output
    rows = mr.get_metric_results()[0]["rows"]
    assert rows, "工具排行应有数据（演示库累积了大量调用）"
    assert all("dim" in row and "value" in row for row in rows)
    # 稳定常量：本项目的检索工具必然在榜（与 API 层测试同口径）
    assert "search_documents" in output


# ============================================================
# 二、Langfuse 源：真数据或降级，二选一
# ============================================================
async def test_langfuse_source_data_or_degraded_never_half():
    """
    小窗口查 token_cost：要么有数据、要么干净降级（degraded + error + 空 rows）。
    不允许"半截状态"（有 error 却说没降级、或降级了却留着旧数据）。
    """
    output = await execute_tool(
        "query_metric",
        {
            "metric": "token_cost",
            "dimension": "model",
            "start_date": "2026-10-15",
            "end_date": "2026-10-16",
        },
    )

    item = mr.get_metric_results()[0]
    if item["degraded"]:
        assert "暂时不可用" in output
        assert item["error"]
        assert item["rows"] == []
    else:
        assert "执行失败" not in output
        for row in item["rows"]:
            assert row["value"] >= 0
            assert row["count"] > 0  # 样本数（诚实口径：langfuse 源每行带 count）


# ============================================================
# 三、注入经注册表：拒绝 + 无登记 + 库无损
# ============================================================
async def test_injection_via_registry_rejected_and_db_intact():
    """注入维度从工具入口打进来：被拒、无结构登记；随后正常查询照常出数。"""
    bad = await execute_tool(
        "query_metric",
        {
            "metric": "tool_usage",
            "dimension": "tool_name; DROP TABLE messages; --",
            **WIDE,
        },
    )
    assert "执行失败" in bad
    assert "不支持的维度" in bad
    assert mr.get_metric_results() == []

    # 库毫发无损：同表查询照常（messages 表还在）
    good = await execute_tool(
        "query_metric", {"metric": "message_count", **WIDE}
    )
    assert "执行失败" not in good
    assert mr.get_metric_results()[0]["rows"][0]["value"] > 0


async def test_unknown_metric_via_registry_lists_alternatives():
    """未知指标从工具入口打进来：纠错文本列出可用指标（LLM 的下一条提示词）。"""
    output = await execute_tool(
        "query_metric", {"metric": "user_password_table", **WIDE}
    )
    assert "执行失败" in output
    assert "未知指标" in output
    assert "session_count" in output and "eval_accuracy" in output
    assert mr.get_metric_results() == []


# ============================================================
# 四、跨层一致：工具链 ↔ API 层
# ============================================================
async def test_tool_result_matches_api_layer(api_client):
    """同窗口同指标：工具链查询结果与 API 层查询结果逐位一致（同一出口）。"""
    window = {"start_date": "2026-10-01", "end_date": "2026-10-16"}

    await execute_tool("query_metric", {"metric": "message_count", **window})
    tool_value = mr.get_metric_results()[0]["rows"][0]["value"]

    resp = await api_client.post(
        "/api/metrics/query", json={"metric": "message_count", **window}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0]["value"] == tool_value
