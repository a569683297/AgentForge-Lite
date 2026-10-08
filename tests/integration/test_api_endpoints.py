"""
集成：HTTP 接口层（真打 FastAPI 应用；**只读**）
=================================================
标记 `db` —— 这些用例需要真实 Postgres / Redis（本地 compose 起的 db/redis）。

为什么敢打真库：这层要验的正是"路由 → service → ORM → 数据库"整条线通不通，
把 DB 换成 mock 就等于**把要验的东西自己删掉了**。
代价是这些用例必须**只读**：不建、不改、不删任何数据。
"""

import pytest

pytestmark = pytest.mark.db


# ============================================================
# 健康检查
# ============================================================


async def test_health_reports_dependencies(api_client):
    """`/api/health` 是 Docker 健康检查探针用的那个端点（compose 里直接指向它）。"""
    resp = await api_client.get("/api/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "ok"
    assert body["deps"]["api"] == "ok"
    assert body["deps"]["redis"] == "ok"  # 真去 ping 了 Redis，不是写死的


# ============================================================
# 工具清单（D27）
# ============================================================


async def test_tools_endpoint_has_four_sections(api_client):
    """返回结构是前端"工具注册表"页的数据源，四个键各有分工，缺一不可。"""
    resp = await api_client.get("/api/tools")
    assert resp.status_code == 200

    body = resp.json()
    assert {"total", "by_source", "servers", "tools"} <= set(body)
    assert body["total"] == len(body["tools"])


async def test_by_source_partitions_the_flat_tool_list(api_client):
    """
    ★ 内部一致性断言（不依赖工具总数）：`by_source` 必须**不多不少**地
    把 `tools` 里的名字分完 —— 它是按 source 分组的，不是另一份独立清单。

    比"断言有 13 个工具"稳得多：后者会随接入的 server 变化而红，
    而这条永远成立（除非分组逻辑 itself 出错）。
    """
    body = (await api_client.get("/api/tools")).json()

    flat = sorted(t["name"] for t in body["tools"])
    grouped = sorted(name for names in body["by_source"].values() for name in names)
    assert flat == grouped

    # 每个 tools 条目的 source 必须与它落在的分组一致
    for tool in body["tools"]:
        assert tool["name"] in body["by_source"][tool["source"]]


async def test_tool_entries_are_json_serializable_and_carry_source(api_client):
    """`describe_tools()` 的每一条都必须能进 JSON（不能混进函数对象），且带 source。"""
    body = (await api_client.get("/api/tools")).json()
    for tool in body["tools"]:
        assert isinstance(tool["name"], str)
        assert isinstance(tool["source"], str)
        assert isinstance(tool["parameters"], dict)


async def test_servers_block_answers_a_different_question(api_client):
    """
    `tools` 说"现在有哪些工具"，`servers` 说"这些工具的主人现在什么情况"。
    两者可以不一致 —— 那种不一致本身就是要看见的信息。
    """
    body = (await api_client.get("/api/tools")).json()
    assert isinstance(body["servers"], list)


# ============================================================
# 评测接口（D21 / D22）
# ============================================================


async def test_eval_cases_list_returns_list(api_client):
    resp = await api_client.get("/api/eval/cases")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_eval_cases_reject_invalid_category_with_422(api_client):
    """
    ★ 非法类别必须 422（由 `Literal` 类型自动拦），**不能**静默查空返回 `[]`。

    后者会伪装成"这个类别暂时没有题"，而真相是参数打错了 ——
    这类"看起来正常的错答案"最难查。
    """
    resp = await api_client.get("/api/eval/cases", params={"category": "不存在的类别"})
    assert resp.status_code == 422


async def test_missing_case_returns_404_not_500(api_client):
    """不存在的 case_key 是**用户输入问题**（404），不是服务端故障（500）。"""
    resp = await api_client.get("/api/eval/cases/NOPE-999")
    assert resp.status_code == 404


async def test_dataset_fingerprint_is_stable_across_calls(api_client):
    """
    ★ "冻结"这条纪律的**可验证出口**：D23 报告里写一个指纹，
    它必须与这里读到的一致；不一致就说明题目被改过、报告作废。
    连读两次必须完全相同（否则连"一次运行内自洽"都做不到）。
    """
    first = (await api_client.get("/api/eval/dataset")).json()
    second = (await api_client.get("/api/eval/dataset")).json()
    assert first == second


# ============================================================
# 会话接口（D14）
# ============================================================


async def test_sessions_list_returns_list(api_client):
    resp = await api_client.get("/api/sessions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_unknown_route_is_404(api_client):
    """未知路径要干净地 404（被全局异常处理器兜成 500 就说明路由层有问题）。"""
    resp = await api_client.get("/api/这个路径不存在")
    assert resp.status_code == 404
