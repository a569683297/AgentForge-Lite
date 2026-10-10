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


# ============================================================
# 对话接口（D11；D36 新增防回归）
# ============================================================


async def test_chat_forwards_every_chatresult_field_to_http(api_client, monkeypatch):
    """
    ★ 防回归（D36）：`/api/chat` 必须把 `ChatResult` 的**每个字段**原样搬进响应。

    D36 踩过的坑：路由构造 `ChatResponse(...)` 时**漏传 `tool_calls`**。该字段有
    `default_factory=list`，漏传**不报错、不崩溃** —— 响应里 `tool_calls` 恒为 `[]`，
    前端「工具卡片」永远不显示（刷新后从 PG 重读才出现，伪装成前端 bug）。
    这类"给模型加字段、忘了同步搬"是**静默**的，只能靠跨层比对抓。

    ⚠ 为什么不能只断言"响应键集合 == 模型字段集合"：
      pydantic / FastAPI 序列化**会带上默认值** —— 漏传的字段在 JSON 里照样出现
      （只是值退回了默认）。所以键集合比对对这个 bug **恒真**，抓不到。
    正确做法：把 `run_agent` 换成返回**每个字段都带哨兵值**的桩，
    再断言哨兵能原样读回 —— 漏传哪个字段，哪个就退回默认值 → 这边红。
    """
    from app.api import chat as chat_module
    from app.schemas.chat import ChatResponse, ChatResult, SourceItem

    fake = ChatResult(
        answer="哨兵回答",
        sources=[
            SourceItem(
                index=7,
                source="SENTINEL_DOC",
                content="哨兵片段",
                similarity=0.4242,
                page_ref="p.42",
            )
        ],
        invalid_citations=[13],
        tool_calls=["SENTINEL_TOOL"],
    )

    async def _fake_run_agent(session_id, message):  # 签名对齐路由里的调用
        return fake

    monkeypatch.setattr(chat_module, "run_agent", _fake_run_agent)

    resp = await api_client.post("/api/chat", json={"message": "任意一句话"})
    assert resp.status_code == 200
    body = resp.json()

    # 逐字段原样读回：漏传哪个，哪个就退回默认值 —— 下行就是 D36 的真 bug 所在
    assert body["answer"] == "哨兵回答"
    assert body["invalid_citations"] == [13]
    assert body["tool_calls"] == ["SENTINEL_TOOL"]
    assert [s["source"] for s in body["sources"]] == ["SENTINEL_DOC"]
    assert body["sources"][0]["page_ref"] == "p.42"

    # 轻量一致性：不出现模型里没有的键、也不缺键。
    # （对"默认值兜底"型 bug 恒真，真正的守卫是上面的哨兵断言。）
    assert set(body) == set(ChatResponse.model_fields)

    # ★ 元断言：桩必须覆盖 ChatResult 的**全部**字段。
    # 将来给 ChatResult 加字段时这条会红，逼你回来补哨兵 —— 不让新字段裸奔。
    covered = {"answer", "sources", "invalid_citations", "tool_calls"}
    assert covered == set(ChatResult.model_fields), (
        "ChatResult 字段变了：请在桩里为新字段补哨兵值，并加一条能原样读回的断言"
    )
