"""
集成：跨用户数据隔离（多租户的边界在哪一格）
=============================================
账号体系最容易出的错，不是"登录不上"，而是**登录得上但看得见别人的数据**。
这类错误有个共同特征：**不报错、不报警、页面上一切正常** ——
只是 B 的文档列表里悄悄多了 A 的员工手册。

所以这里的断言都是"看不见"型的：
  · 列表里没有别人的行（而不是"报了 403"）
  · 别人的 id 一律 404（而不是 403 —— 403 等于承认"这个 id 存在"）
  · 别人的会话不能被写（聊天接口的归属校验必须在跑图之前）

最后一条 `test_bm25_statistics_do_not_leak_across_owners` 是本文件里最"绕"、
也最值得留的一条：它守的不是"召回范围"，而是**统计量**的隔离。
详见该用例的 docstring。

数据准备走 service 层直写（不经 HTTP 上传）：上传会触发解析 + 向量化，
而这里要验的是"归属过滤"，不是入库链路 —— 把无关环节拉进来只会让用例变慢变脆。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.db


# ============================================================
# 造数与清理
# ============================================================


async def _make_document(owner_id, filename: str, text: str) -> uuid.UUID:
    """
    直接插一份 ready 文档 + 一个切片（不走向量化）。

    `embedding` 填全 0 向量：列是 NOT NULL，而本文件的用例都只走关键词/列表路径，
    不需要真实向量。填 0 而不是随机数的原因 —— 万一将来有人拿它跑向量检索，
    全 0 会得到"所有文档等距"这种一眼可辨的结果，比随机数更容易发现用错了数据。
    """
    from app.config import settings
    from app.core.db import async_session_factory
    from app.models.document import Document, DocumentStatus
    from app.models.document_chunk import DocumentChunk
    from app.services.tokenizer import tokenize_to_string

    async with async_session_factory() as session:
        doc = Document(
            filename=filename,
            file_type="md",
            owner_id=owner_id,
            status=DocumentStatus.READY,
            chunk_count=1,
        )
        session.add(doc)
        await session.flush()          # 拿到 doc.id
        session.add(
            DocumentChunk(
                document_id=doc.id,
                chunk_index=0,
                content=text,
                content_tokens=tokenize_to_string(text),
                embedding=[0.0] * settings.embedding_dim,
            )
        )
        await session.commit()
        return doc.id


async def _drop_documents(document_ids: list[uuid.UUID]) -> None:
    """删文档（切片靠外键 CASCADE 带走）。"""
    from sqlalchemy import delete

    from app.core.db import async_session_factory
    from app.models.document import Document

    if not document_ids:
        return
    async with async_session_factory() as session:
        await session.execute(delete(Document).where(Document.id.in_(document_ids)))
        await session.commit()


async def _drop_session(session_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    from app.core.db import async_session_factory
    from app.models.session import Session

    async with async_session_factory() as session:
        await session.execute(delete(Session).where(Session.id == session_id))
        await session.commit()


@pytest.fixture
async def docs():
    """收集本用例建的文档 id，结束统一删。"""
    created: list[uuid.UUID] = []
    yield created
    await _drop_documents(created)


# ============================================================
# 一、文档：列表 / 详情 / 删除
# ============================================================


async def test_document_list_shows_only_own(api_client, user_client, docs, admin_user, normal_user):
    """
    ★ 两个用户各有一份文档，各自的列表里只能看到自己那份。

    这是隔离的"主断言"。如果只写一条"B 看不到 A 的"，很容易被
    "列表干脆返回空"这种错误实现蒙混过关 —— 所以两个方向都断言。
    """
    admin_doc = await _make_document(admin_user.id, "admin-only.md", "管理员的知识库内容")
    user_doc = await _make_document(normal_user.id, "user-only.md", "普通用户的知识库内容")
    docs.extend([admin_doc, user_doc])

    user_list = (await user_client.get("/api/documents")).json()
    admin_list = (await api_client.get("/api/documents")).json()

    user_ids = {row["id"] for row in user_list}
    admin_ids = {row["id"] for row in admin_list}

    assert str(user_doc) in user_ids
    assert str(admin_doc) not in user_ids
    assert str(admin_doc) in admin_ids
    assert str(user_doc) not in admin_ids


async def test_document_preview_of_other_user_is_404(user_client, docs, admin_user):
    """★ 别人的文档详情 → 404（不是 403：403 等于承认"这个 id 存在"）。"""
    other = await _make_document(admin_user.id, "secret.md", "别人不该看到的正文")
    docs.append(other)

    resp = await user_client.get(f"/api/documents/{other}/preview")

    assert resp.status_code == 404
    # 连正文片段也不能漏（有些实现会先在错误体里带上数据，那就白挡了）
    assert "别人不该看到" not in resp.text


async def test_document_delete_of_other_user_is_404_and_keeps_row(
    api_client, user_client, docs, admin_user
):
    """
    ★ 越权删除必须"删不动"，而且**原主还看得见**。

    只断言 404 是不够的：有种错误实现是"先查到了、删了、然后返回 404"。
    所以第二段要回查原主视角，确认数据还在。
    """
    other = await _make_document(admin_user.id, "keep-me.md", "不该被删掉的内容")
    docs.append(other)

    resp = await user_client.delete(f"/api/documents/{other}")
    assert resp.status_code == 404

    still_there = await api_client.get(f"/api/documents/{other}/preview")
    assert still_there.status_code == 200


# ============================================================
# 二、会话：列表 / 消息 / 聊天入口
# ============================================================


async def test_session_list_shows_only_own(api_client, user_client, admin_user, normal_user):
    """会话列表同样按 owner 过滤（这里靠 `ensure_session` 造两条真会话）。"""
    from app.services.session_service import ensure_session

    admin_sid = uuid.uuid4()
    user_sid = uuid.uuid4()
    await ensure_session(admin_sid, title_hint="管理员开的会话", owner_id=admin_user.id)
    await ensure_session(user_sid, title_hint="普通用户的会话", owner_id=normal_user.id)
    try:
        user_ids = {row["id"] for row in (await user_client.get("/api/sessions")).json()}
        admin_ids = {row["id"] for row in (await api_client.get("/api/sessions")).json()}

        assert str(user_sid) in user_ids
        assert str(admin_sid) not in user_ids
        assert str(admin_sid) in admin_ids
        assert str(user_sid) not in admin_ids
    finally:
        await _drop_session(admin_sid)
        await _drop_session(user_sid)


async def test_session_messages_of_other_user_is_404(user_client, admin_user):
    """★ 别人的会话历史 → 404（消息里往往是最敏感的东西：用户问过什么）。"""
    from app.services.session_service import append_messages, ensure_session

    sid = uuid.uuid4()
    await ensure_session(sid, title_hint="管理员的私人会话", owner_id=admin_user.id)
    await append_messages(
        sid,
        [
            {"role": "user", "content": "我的工资是多少"},
            {"role": "assistant", "content": "（假装这是回答）"},
        ],
        owner_id=admin_user.id,
    )
    try:
        resp = await user_client.get(f"/api/sessions/{sid}/messages")
        assert resp.status_code == 404
        assert "我的工资" not in resp.text
    finally:
        await _drop_session(sid)


async def test_chat_rejects_others_session_with_404(user_client, admin_user, mock_llm):
    """
    ★ 往别人的会话里发消息 → 404，且**一个字都没写进去**。

    这是最危险的一种越权（写），因为它的后果不是"看到不该看的"，
    而是"把内容塞进了别人的上下文"。归属校验必须发生在跑图之前。

    `mock_llm` 是保险：万一校验被挪到跑图之后，用例不会因为真打外部 API
    而变成一个"慢且不稳定"的失败，而是干脆利落地红在这条断言上。
    """
    from app.services.session_service import ensure_session, get_messages

    sid = uuid.uuid4()
    await ensure_session(sid, title_hint="管理员的会话", owner_id=admin_user.id)
    try:
        resp = await user_client.post(
            "/api/chat", json={"message": "插一句别人的上下文", "session_id": str(sid)}
        )
        assert resp.status_code == 404

        # 校验的核心：库里没有新增任何消息
        rows = await get_messages(sid, limit=10, owner_id=None)
        assert rows == []
        assert mock_llm.calls == []
    finally:
        await _drop_session(sid)


async def test_chat_stream_rejects_others_session_before_opening(
    user_client, admin_user, mock_llm
):
    """
    ★ 流式接口必须在**开流之前**拒绝，返回干净的 404。

    开流之后 HTTP 状态码已经是 200，越权只能表现为一条 error 帧 ——
    前端得先判断"这是不是权限问题"，而它本该是一个 404。
    """
    from app.services.session_service import ensure_session

    sid = uuid.uuid4()
    await ensure_session(sid, title_hint="管理员的会话", owner_id=admin_user.id)
    try:
        resp = await user_client.post(
            "/api/chat/stream", json={"message": "越权", "session_id": str(sid)}
        )
        assert resp.status_code == 404
        # 没有 meta 帧 = 流确实没开
        assert not resp.text.startswith("data:")
    finally:
        await _drop_session(sid)


# ============================================================
# 三、模型：别人的端点（及其 key）不该被改
# ============================================================


async def test_model_of_other_user_is_invisible_and_immutable(
    api_client, user_client, normal_user
):
    """
    ★ 模型注册表的隔离，重点不在"看不见"，而在**改不动**。

    客户端可以自己传 model_id —— 不校验归属的话，B 能把 A 的 base_url
    改成自己的服务器（A 下一次对话的 key 就发给 B 了）。
    """
    created = await user_client.post(
        "/api/models",
        json={
            "name": f"pytest-iso-{uuid.uuid4().hex[:6]}",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-normal-user-key-1234",
            "model": "m",
        },
    )
    assert created.status_code == 201, created.text
    model_id = created.json()["id"]
    try:
        # 管理员看不见它
        admin_models = (await api_client.get("/api/models")).json()["models"]
        assert model_id not in {m["id"] for m in admin_models}

        # 也改不动、删不掉 —— 一律 404
        assert (
            await api_client.patch(f"/api/models/{model_id}", json={"name": "被改了"})
        ).status_code == 404
        assert (await api_client.delete(f"/api/models/{model_id}")).status_code == 404

        # 原主视角：名字没被改，还在列表里
        mine = (await user_client.get("/api/models")).json()["models"]
        still = next(m for m in mine if m["id"] == model_id)
        assert still["name"] == created.json()["name"]
    finally:
        await user_client.delete(f"/api/models/{model_id}")


# ============================================================
# 四、BM25 统计量隔离（最容易被忽略的一格）
# ============================================================


async def test_bm25_statistics_do_not_leak_across_owners(
    silence_langfuse, normal_user, admin_user, docs
):
    """
    ★ 别人的语料不该影响**我的分数**。

    为什么这条要单独立一个用例：
      BM25 的 SQL 有三段参与计算 —— corpus（全库文档数 N 与平均长度 avgdl）、
      candidates（谁被召回）、df（每个查询词的文档频率）。
      只给 candidates 加 owner 条件，召回范围是对的，**排序却是错的**：
      IDF = ln(1 + (N - df + 0.5) / (df + 0.5))，当"别人文档里也有这个词"时
      df 变大、N 也变大，算出来的分被悄悄压低。结果不报错、不越权、
      只是"我搜同一个东西，排序和别人库里有多少相关文档有关"。
      这类问题在数据隔离里叫**统计量侧信道**。

    测法：先只放我自己的文档测一次分数，再往别人名下塞 5 份含同一个词的文档，
    再测一次 —— **分数必须一模一样**。三个 CTE 缺任何一个，这个断言都会红。
    """
    from app.services.retrieval_service import search_keywords

    term = "纠缠"
    mine = await _make_document(normal_user.id, "mine.md", f"{term}实验的记录与结论")
    docs.append(mine)

    alone = await search_keywords("纠缠实验", top_k=10, owner_id=normal_user.id)
    assert alone, "自己的文档必须能被打分（否则后面的比较没有意义）"
    baseline = alone[0]["score"]

    # 别人的语料：同一个词出现 5 次（df 从 1 变 6、N 从 1 变 6）
    neighbours = [
        await _make_document(admin_user.id, f"other-{i}.md", f"{term}{term}{term} 与我无关的语料")
        for i in range(5)
    ]
    docs.extend(neighbours)

    after = await search_keywords("纠缠实验", top_k=10, owner_id=normal_user.id)

    # ① 召回范围没被污染
    assert all(row["source"] == "mine.md" for row in after)
    # ② ★ 分数没有因为别人有同类文档而变化 —— 三个 CTE 都按 owner 过滤了
    assert after[0]["score"] == pytest.approx(baseline, rel=1e-9), (
        "分数变了，说明 corpus / df 这两个统计量没有按 owner 隔离"
    )
