"""
D11 HTTP 层验证：POST /api/chat
===============================
用 TestClient 直接打 ASGI 应用（不启 uvicorn），验证 HTTP 边界上的行为：

  H1. 不传 session_id → 服务端新建并回传；回答带 [n]，sources 能映射回原文
  H2. 传 session_id → 服务端原样沿用（多轮对话靠它串起来）
  H3. 参数校验 → 空 message / 缺 message / 超长 / 非法 session_id 被 422 拒绝
  H4. 响应结构 → session_id / answer / sources / invalid_citations 四字段齐全

用法：
    uv run python -m scripts.d11_http_verify

⚠ 每个用例用独立 session_id（带 uuid 后缀），避免命中上一轮历史：
  这是 D11 踩过的坑——会话 ID 写死会让 LLM 直接从记忆作答、不再调工具，
  表现为 sources 为空，看起来像"收集器坏了"。
"""

import uuid

from fastapi.testclient import TestClient

from app.main import app


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def new_session(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ============================================================
# H1. 不传 session_id
# ============================================================
def case_h1_new_session(client: TestClient) -> bool:
    """服务端新建会话，且回答能通过 sources 定位回原文"""
    section("H1. 不传 session_id → 服务端新建会话 + 引用可定位")

    resp = client.post("/api/chat", json={"message": "「琥珀」这个项目是哪个组负责的？"})
    data = resp.json()

    print(f"  HTTP 状态 = {resp.status_code}")
    sid = data.get("session_id", "")
    print(f"  session_id = {sid}  (长度 {len(sid)})")
    print(f"  answer = {data.get('answer', '')[:150]}")
    print("  ---- sources（前端拿到的映射表）----")
    for s in data.get("sources", []):
        preview = s["content"][:36].replace("\n", " ")
        print(f"    [{s['index']}] {s['source']}  相关度 {s['similarity']}  「{preview}…」")
    print(f"  invalid_citations = {data.get('invalid_citations')}")

    ok = (
        resp.status_code == 200
        and len(sid) == 32                      # uuid4().hex 是 32 位
        and len(data.get("sources", [])) > 0    # 来源表非空，[n] 才能映射
        and "星河" in data.get("answer", "")     # 答案来自文档而非模型编造
    )
    print(f"  [判定] {'✅ 服务端新建会话，回答可定位原文' if ok else '❌ 未达预期'}")
    return ok


# ============================================================
# H2. 传 session_id（多轮）
# ============================================================
def case_h2_reuse_session(client: TestClient) -> bool:
    """服务端原样沿用传入的 session_id，两轮都返回 200"""
    section("H2. 传 session_id → 沿用同一会话")

    sid = new_session("d11-http")

    r1 = client.post(
        "/api/chat",
        json={"message": "「翡翠」这个项目是哪个组负责的？", "session_id": sid},
    )
    d1 = r1.json()
    print(f"  第 1 轮 状态={r1.status_code}  回传 session_id={d1.get('session_id')}")
    print(f"          answer={d1.get('answer', '')[:110]}")

    r2 = client.post(
        "/api/chat",
        json={"message": "那这个项目的负责人是谁？", "session_id": sid},
    )
    d2 = r2.json()
    print(f"  第 2 轮 状态={r2.status_code}  回传 session_id={d2.get('session_id')}")
    print(f"          answer={d2.get('answer', '')[:110]}")
    print(f"          sources={len(d2.get('sources', []))} 条"
          "（第二轮若直接沿用历史作答，这里会是 0 —— D11 已知缺陷，见教程 §9 坑 5）")

    ok = (
        r1.status_code == 200
        and r2.status_code == 200
        and d1.get("session_id") == sid
        and d2.get("session_id") == sid
        and bool(d2.get("answer"))
    )
    print(f"  [判定] {'✅ session_id 原样沿用，多轮均 200' if ok else '❌ 未达预期'}")
    return ok


# ============================================================
# H3. 参数校验
# ============================================================
def case_h3_validation(client: TestClient) -> bool:
    """非法请求在 pydantic 层就被拒，不进入业务逻辑"""
    section("H3. 参数校验（非法输入 422）")

    cases = [
        ("空 message", {"message": ""}),
        ("缺 message", {}),
        ("message 超长(2001)", {"message": "x" * 2001}),
        ("session_id 非法字符", {"message": "你好", "session_id": "bad id!"}),
        ("session_id 超长(65)", {"message": "你好", "session_id": "a" * 65}),
    ]

    ok = True
    for name, payload in cases:
        r = client.post("/api/chat", json=payload)
        passed = r.status_code == 422
        ok = ok and passed

        loc = "-"
        detail = r.json().get("detail")
        if isinstance(detail, list) and detail:
            loc = ".".join(str(x) for x in detail[0].get("loc", []))
        print(f"  {'✅' if passed else '❌'} {name:22} → HTTP {r.status_code}  loc={loc}")

    print(f"  [判定] {'✅ 非法输入全部被拒（未进入业务逻辑）' if ok else '❌ 有非法输入漏过校验'}")
    return ok


# ============================================================
# H4. 响应结构
# ============================================================
def case_h4_response_shape(client: TestClient) -> bool:
    """四个字段齐全，且是 JSON（闲聊场景，快）"""
    section("H4. 响应结构完整性")

    r = client.post(
        "/api/chat",
        json={"message": "你好", "session_id": new_session("d11-shape")},
    )
    data = r.json()
    expected = {"session_id", "answer", "sources", "invalid_citations"}
    keys = set(data.keys())

    print(f"  响应字段   = {sorted(keys)}")
    print(f"  期望字段   = {sorted(expected)}")
    print(f"  Content-Type = {r.headers.get('content-type')}")
    print(f"  sources 类型 = {type(data.get('sources')).__name__}，"
          f"invalid_citations 类型 = {type(data.get('invalid_citations')).__name__}")

    ok = expected <= keys and "application/json" in r.headers.get("content-type", "")
    print(f"  [判定] {'✅ 四字段齐全且为 JSON' if ok else '❌ 结构不符'}")
    return ok


def main() -> None:
    results: list[tuple[str, bool]] = []

    # TestClient 作为上下文管理器使用 → 触发 lifespan（Redis 自检等）
    with TestClient(app) as client:
        results.append(("H1 新建会话 + 引用可定位", case_h1_new_session(client)))
        results.append(("H2 多轮沿用会话", case_h2_reuse_session(client)))
        results.append(("H3 参数校验", case_h3_validation(client)))
        results.append(("H4 响应结构", case_h4_response_shape(client)))

    section("汇总")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n  总计 {sum(1 for _, ok in results if ok)}/{len(results)} 通过")


if __name__ == "__main__":
    main()
