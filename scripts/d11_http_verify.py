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

⚠ 本脚本**自己播种知识库**（D12 补）：
  之前它默默依赖「上一个脚本跑完留下的数据」，单独跑或换顺序跑就会失败 ——
  典型的现象是 H1 报错却看不出原因。每个验证脚本都该能独立跑通。
"""

import asyncio
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.services.document_service import (
    count_documents_by_prefix,
    delete_documents_by_prefix,
    ingest_texts,
)

# 本脚本造的文档统一带此前缀（2026-09-28 新增）—— 清理时的作用域边界。
# 原先调 delete_all_documents() 清空全库，会删光 D21 的长期语料。
# ⚠ 与 d11_citation_verify 用**不同的**前缀：两个脚本各管各的数据，
#   否则后跑的会把先跑的产物一起收走（虽然内容一样，但作用域不清）。
VERIFY_PREFIX = "d11-http"
# 与 d11_citation_verify 用同一批虚构事实，保证两个脚本对同一份知识库做验证
DOCS = [
    (
        f"{VERIFY_PREFIX}-alpha.md",
        "公司内部项目管理规定：代号为「琥珀」的项目由星河算法组负责，项目周期两年，"
        "负责人为林工。该项目组直接向 CTO 汇报。",
    ),
    (
        f"{VERIFY_PREFIX}-beta.md",
        "公司内部项目管理规定：代号为「翡翠」的项目由山海基础组负责，项目周期一年，"
        "负责人为周工。该项目组直接向 CTO 汇报。",
    ),
]


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def new_session() -> str:
    """
    新的会话 ID。

    D14：改成标准 UUID 字符串（36 位带横杠）。服务端的 session_id 类型收口成
    uuid.UUID 了，像 "d11-http-3f2a1b9c" 这种自造字符串会被 pydantic 直接 422。
    HTTP 层传的仍是字符串（JSON 里没有 uuid 类型），所以这里返回 str。
    """
    return str(uuid.uuid4())


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
        and len(sid) == 36                      # D14 起是标准 UUID：36 位带横杠
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

    sid = new_session()

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

    # D14 新增：用「无横杠的 32 位」写法传同一个会话。
    # 这验证的是本次改动的核心保证 —— 两种写法归一化到同一个 id，
    # 也就是同一个 Redis key、同一行 sessions 记录（不会再分裂成两个会话）。
    r3 = client.post(
        "/api/chat",
        json={"message": "它的负责人是谁？", "session_id": sid.replace("-", "")},
    )
    d3 = r3.json()
    print(f"  第 3 轮（无横杠写法）状态={r3.status_code}  回传 session_id={d3.get('session_id')}")
    print(f"          归一化后与原 id 相等：{d3.get('session_id') == sid}")

    ok = (
        r1.status_code == 200
        and r2.status_code == 200
        and r3.status_code == 200
        and d1.get("session_id") == sid
        and d2.get("session_id") == sid
        and d3.get("session_id") == sid        # ← 无横杠写法必须归一化回带横杠
        and bool(d2.get("answer"))
    )
    print(
        f"  [判定] "
        f"{'✅ session_id 原样沿用，无横杠写法归一化到同一会话' if ok else '❌ 未达预期'}"
    )
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
        # D14：session_id 类型收口为 uuid.UUID —— 非 uuid 字符串一律 422
        #（旧正则 ^[A-Za-z0-9_-]{1,64}$ 会把 "abc" 这种值放行，属于漏网）
        ("session_id 非 UUID", {"message": "你好", "session_id": "abc"}),
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
        json={"message": "你好", "session_id": new_session()},
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


async def prepare_kb() -> None:
    """播种知识库，让本脚本可以独立运行。"""
    section("准备知识库（本脚本自己播种，只清自己的数据）")
    await delete_documents_by_prefix(VERIFY_PREFIX)
    for filename, text in DOCS:
        _, count = await ingest_texts([text], filename=filename)
        print(f"  入库 {filename} → {count} 个切片")

    # 关键：用完立刻清空连接池。
    # 下面 TestClient 会在**另一个线程里另起一个事件循环**，
    # 池里残留的连接绑定在刚才那个已关闭的 loop 上，复用会直接报
    # "attached to a different loop"。dispose 掉，让新 loop 建新连接。
    from app.core.db import engine

    await engine.dispose()


async def cleanup_kb() -> None:
    """跑完把自己的语料收干净 —— 别给 D22/D23 的检索评测留下会命中的垃圾。"""
    await delete_documents_by_prefix(VERIFY_PREFIX)
    left = await count_documents_by_prefix(VERIFY_PREFIX)
    print(f"\n（已清理本脚本语料：{VERIFY_PREFIX}* 残留={left}）")
    if left:
        raise RuntimeError(f"清理不干净：{VERIFY_PREFIX}* 还剩 {left} 份")


def main() -> None:
    asyncio.run(prepare_kb())

    results: list[tuple[str, bool]] = []

    # TestClient 作为上下文管理器使用 → 触发 lifespan（Redis 自检等）
    with TestClient(app) as client:
        results.append(("H1 新建会话 + 引用可定位", case_h1_new_session(client)))
        results.append(("H2 多轮沿用会话", case_h2_reuse_session(client)))
        results.append(("H3 参数校验", case_h3_validation(client)))
        results.append(("H4 响应结构", case_h4_response_shape(client)))

        # ⚠ 清理必须**在 TestClient 自己的 loop 内**完成（2026-09-28 补）。
        #   第一版写成 `asyncio.run(cleanup_kb())`（出了 with 块再收尾），
        #   结果是 exit=1：新 loop 拿到绑在 TestClient loop 上的连接 →
        #   `got Future ... attached to a different loop`。
        #   改成在块内 dispose 之后，又变成 exit=134（SIGABRT，
        #   `libc++abi: recursive_mutex lock failed`）—— 功能全对、"汇总 4/4 通过"
        #   都打出来了，崩在解释器退出阶段：多起了一个 loop 之后，
        #   某些 C 层（tokenizer / onnxruntime）的线程在析构时被打断。
        #   → 结论：**这里的活别另起 loop**，用 portal 把它送回 TestClient 的 loop 执行。
        #   这与 d14_sessions_verify 用 client.portal.call(engine.dispose) 是同一条纪律。
        client.portal.call(cleanup_kb)

    section("汇总")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n  总计 {sum(1 for _, ok in results if ok)}/{len(results)} 通过")


if __name__ == "__main__":
    main()
