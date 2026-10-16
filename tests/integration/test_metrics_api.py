"""
集成：语义层接口（真打 FastAPI 应用 + 真实 Postgres，**只读**）
================================================================
标记 `db`（需要真实 Postgres —— 指标的意义就是"查到真数据"，
把库换成 mock 等于把要验的东西删掉了）。本组用例**不写库**：
查询接口天然只读，纪律的成本为零。

数据断言的分寸：断言"结构 + 稳定常量"（三种检索配置是 RETRIEVER_CONFIGS 的
冻结枚举、search_documents 是本项目检索工具），不钉精确数值 ——
数值会随演示数据增长，钉死它只会让测试隔几天就红一次（"假报警"的老路）。

一个跨指标一致性用例（test_volume_sum_equals_total）值得说明：
`message_volume`（按天分组）与 `message_count`（聚合）读同一张表，
**同一窗口下逐日之和必须恰等于总数** —— 它同时验证两个模板的
行数与时间窗语义没写歪，是比"各有各的断言"更强的检验。

Langfuse 源（块 2）的断言口径不同：它们走外部读通道（限流紧、数据多少取决于
环境里跑过多少对话），所以只钉**结构契约 + 降级语义**（PRD §9.10.5 约束 1/3），
不断言"必须有数据"——那会让测试随环境波动，红得毫无信息量。
"""

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.db

# 宽窗口：覆盖演示数据的全部时间跨度（2026-09 起）
WIDE = {"start_date": "2020-01-01", "end_date": "2030-01-01"}


async def _query(api_client, **payload):
    payload = {**WIDE, **payload}
    return await api_client.post("/api/metrics/query", json=payload)


# ============================================================
# 一、指标清单（自描述出口）
# ============================================================
async def test_list_metrics_returns_catalog(api_client):
    resp = await api_client.get("/api/metrics")
    assert resp.status_code == 200, resp.text
    items = {item["name"]: item for item in resp.json()}
    assert {"session_count", "message_count", "message_volume", "tool_usage"} <= set(items)
    assert items["tool_usage"]["dimensions"] == ["tool_name"]
    assert items["session_count"]["dimensions"] == []     # 聚合指标：无维度
    assert items["eval_accuracy"]["source"] == "sql"


# ============================================================
# 二、聚合指标（无维度）：会话 / 消息总数
# ============================================================
async def test_session_count_returns_positive_total(api_client):
    resp = await _query(api_client, metric="session_count")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "sql" and body["dimension"] is None
    assert len(body["rows"]) == 1
    assert body["rows"][0]["value"] > 0


async def test_message_count_returns_positive_total(api_client):
    resp = await _query(api_client, metric="message_count")
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0]["value"] > 0


async def test_aggregate_metric_accepts_empty_dimension(api_client):
    """dimension="" 归一为"不分组"（LLM 与前端都可能把不分组写成空串）。"""
    resp = await _query(api_client, metric="session_count", dimension="")
    assert resp.status_code == 200, resp.text


# ============================================================
# 三、分组指标：曲线 / 排行 / 评测
# ============================================================
async def test_message_volume_by_day_sorted(api_client):
    resp = await _query(api_client, metric="message_volume", dimension="day")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert rows, "至少有一个自然日有消息"
    dims = [r["dim"] for r in rows]
    assert dims == sorted(dims), "按日期的升序输出（前端画曲线直接用）"
    assert all(r["value"] > 0 for r in rows)


async def test_volume_sum_equals_total(api_client):
    """跨指标一致性：逐日消息量之和 == 消息总数（同一窗口、同一张表）。"""
    daily = (await _query(api_client, metric="message_volume", dimension="day")).json()["rows"]
    total = (await _query(api_client, metric="message_count")).json()["rows"][0]["value"]
    assert sum(r["value"] for r in daily) == total


async def test_tool_usage_top_is_search_documents(api_client):
    resp = await _query(api_client, metric="tool_usage", dimension="tool_name")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert rows[0]["dim"] == "search_documents"           # 检索工具是绝对主力（实测 218 次）
    assert rows == sorted(rows, key=lambda r: -r["value"])
    assert all(r["dim"] for r in rows)                    # 无空名字行（JSONB 展开已过滤）


async def test_eval_accuracy_by_config(api_client):
    resp = await _query(api_client, metric="eval_accuracy", dimension="config_name")
    assert resp.status_code == 200, resp.text
    rows = {r["dim"]: r for r in resp.json()["rows"]}
    assert {"pure_vector", "hybrid", "hybrid_rerank"} <= set(rows)   # 三种检索配置冻结枚举
    for row in rows.values():
        assert 0 <= row["value"] <= 1
        assert row["runs"] > 0


async def test_eval_dimension_scores_three_rows(api_client):
    resp = await _query(api_client, metric="eval_dimension_scores", dimension="score_type")
    assert resp.status_code == 200, resp.text
    rows = {r["dim"]: r for r in resp.json()["rows"]}
    assert set(rows) == {"correctness", "faithfulness", "completeness"}
    for row in rows.values():
        assert 0 <= row["value"] <= 5
        assert row["scored_runs"] > 0


async def test_eval_failures_by_category(api_client):
    resp = await _query(api_client, metric="eval_failures_by_category", dimension="category")
    assert resp.status_code == 200, resp.text
    rows = {r["dim"]: r for r in resp.json()["rows"]}
    assert set(rows) == {"doc_qa", "cross_doc", "tool_call"}
    assert sum(r["fail_count"] for r in rows.values()) > 0    # 失败行存在才叫"失分统计"
    assert all(r["total"] > 0 for r in rows.values())


# ============================================================
# 四、参数校验与注入防护（全部 400，且消息可行动）
# ============================================================
async def test_unknown_metric_400(api_client):
    resp = await _query(api_client, metric="profit_margin")
    assert resp.status_code == 400
    assert "未知指标" in resp.json()["detail"]


async def test_invalid_dimension_400(api_client):
    resp = await _query(api_client, metric="tool_usage", dimension="passed")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "tool_name" in detail          # 列出可用维度


async def test_dimension_on_aggregate_metric_400(api_client):
    resp = await _query(api_client, metric="session_count", dimension="day")
    assert resp.status_code == 400
    assert "聚合指标" in resp.json()["detail"]


async def test_bad_date_format_400(api_client):
    resp = await _query(api_client, metric="message_count", start_date="2026/09/01")
    assert resp.status_code == 400
    assert "YYYY-MM-DD" in resp.json()["detail"]


async def test_start_after_end_400(api_client):
    resp = await _query(
        api_client, metric="message_count", start_date="2026-10-16", end_date="2026-10-01"
    )
    assert resp.status_code == 400


async def test_injection_dimension_rejected_and_db_intact(api_client):
    """注入维度：400 拒绝；随后发起一次正常查询证明数据面毫发无损。"""
    resp = await _query(api_client, metric="tool_usage", dimension="tool_name; DROP TABLE messages; --")
    assert resp.status_code == 400
    follow_up = await _query(api_client, metric="message_count")
    assert follow_up.status_code == 200
    assert follow_up.json()["rows"][0]["value"] > 0


# ============================================================
# 五、Langfuse 源（块 2）：运行追踪指标（延迟 / token）
# ============================================================
def _langfuse_window() -> dict:
    """Langfuse 查询的**窄窗口**（最近两天）。

    不能像 SQL 源那样用 WIDE（2020–2030）：外部读通道限流很紧，
    宽窗口 = 试图拉全量历史 = 429。
    """
    today = date.today()
    return {
        "start_date": (today - timedelta(days=1)).isoformat(),
        "end_date": today.isoformat(),
    }


async def test_langfuse_metrics_in_catalog(api_client):
    """两个 langfuse 源指标出现在口径目录里（前端/Agent 不读代码即知）。"""
    resp = await api_client.get("/api/metrics")
    items = {item["name"]: item for item in resp.json()}
    assert items["latency_p95"]["source"] == "langfuse_api"
    assert items["latency_p95"]["dimensions"] == ["name"]
    assert items["token_cost"]["source"] == "langfuse_api"
    assert items["token_cost"]["dimensions"] == ["model"]


async def test_latency_p95_query_contract(api_client):
    """延迟 P95：要么给出合规 rows，要么**明确降级**（读通道不可用时）。

    契约 = 结构（dim/value/count）+ 降级语义（degraded 必带 error、rows 为空）。
    不赌"一定有数据"：那取决于环境里跑过多少对话，红得没有信息量。
    """
    resp = await api_client.post(
        "/api/metrics/query",
        json={**_langfuse_window(), "metric": "latency_p95", "dimension": "name"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "langfuse_api"
    assert body["dimension"] == "name"
    if body["degraded"]:
        assert body["error"], "降级必须给出可转述的原因"
        assert body["rows"] == []
    else:
        for row in body["rows"]:
            assert row["dim"], "分组行的 dim 不得为空"
            assert isinstance(row["value"], (int, float))
            assert isinstance(row["count"], int) and row["count"] >= 1


async def test_token_cost_query_contract(api_client):
    """Token 消耗：同样只钉契约（sum 非负；降级语义与其他 langfuse 指标一致）。"""
    resp = await api_client.post(
        "/api/metrics/query",
        json={**_langfuse_window(), "metric": "token_cost", "dimension": "model"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "langfuse_api"
    if body["degraded"]:
        assert body["error"]
    else:
        for row in body["rows"]:
            assert row["dim"]
            assert row["value"] >= 0


async def test_langfuse_invalid_dimension_400(api_client):
    """换数据源不放松维度白名单（约束 4）：langfuse 指标传非法维度同样 400，
    且**发生在任何网络请求之前**（纯校验路径）。"""
    resp = await api_client.post(
        "/api/metrics/query",
        json={
            "metric": "latency_p95",
            "dimension": "model",
            "start_date": "2026-10-15",
            "end_date": "2026-10-16",
        },
    )
    assert resp.status_code == 400
    assert "name" in resp.json()["detail"]      # 消息列出可用维度
