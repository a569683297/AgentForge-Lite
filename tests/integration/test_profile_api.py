"""
集成：画像接口与跨用户隔离
==========================
这个文件守三件事，每一件的失败后果都不一样：

**一、PATCH 的三态语义**（没传 / 传值 / 传空串）
  表单类接口最经典的坑就是"清空不生效"。这里的用例把三种情况都钉住，
  否则用户改了画像、保存了、结果旧值还在 —— 他会以为是自己没点中。

**二、越权**（404 而不是 403）
  画像是最私密的一类数据。"这个 id 存在但你无权删除"这句话本身就是泄露，
  所以不属于自己的 fact 一律按"不存在"处理。

**三、隔离真的成立**
  两个账号各写一份，互相读不到。这类断言不能只看界面 —— 界面本来就只显示自己的，
  必须**真的用对方的 token 打一次**。
"""

from __future__ import annotations

import uuid

import pytest

from app.services import profile_service

pytestmark = pytest.mark.db


async def _wipe(owner_id) -> None:
    from sqlalchemy import text

    from app.core.db import async_session_factory

    async with async_session_factory() as session:
        await session.execute(
            text("DELETE FROM user_profile_facts WHERE owner_id = :u"), {"u": owner_id}
        )
        await session.execute(
            text("DELETE FROM user_profiles WHERE owner_id = :u"), {"u": owner_id}
        )
        await session.commit()


@pytest.fixture
async def clean(admin_user, normal_user):
    """用例前后都清空两个测试账号的画像（不清理会互相干扰，也会污染真实数据）。"""
    ids = [admin_user.id, normal_user.id]
    for owner_id in ids:
        await _wipe(owner_id)
    yield
    for owner_id in ids:
        await _wipe(owner_id)


# ============================================================
# 一、基本读写与三态语义
# ============================================================


async def test_requires_login(anon_client):
    assert (await anon_client.get("/api/profile")).status_code == 401


async def test_get_returns_field_definitions(api_client, clean):
    response = await api_client.get("/api/profile")
    assert response.status_code == 200
    body = response.json()
    # 字段定义由后端下发 —— 前端不硬编码中文标签，后端加字段时前端不用改
    assert {field["key"] for field in body["fields"]} == {
        "nickname",
        "occupation",
        "tech_stack",
        "answer_style",
    }
    assert body["explicit"] == {}
    assert body["facts"] == []


async def test_patch_roundtrip(api_client, clean):
    saved = await api_client.patch(
        "/api/profile", json={"nickname": "老郭", "occupation": "前端工程师"}
    )
    assert saved.status_code == 200
    assert saved.json()["explicit"] == {"nickname": "老郭", "occupation": "前端工程师"}

    again = (await api_client.get("/api/profile")).json()
    assert again["explicit"] == {"nickname": "老郭", "occupation": "前端工程师"}


async def test_patch_partial_keeps_other_fields(api_client, clean):
    """没传的字段保持原值 —— 否则每次只改一个字段都会把其余三个清空。"""
    await api_client.patch("/api/profile", json={"nickname": "老郭", "occupation": "前端"})
    saved = await api_client.patch("/api/profile", json={"tech_stack": "TypeScript"})
    assert saved.json()["explicit"] == {
        "nickname": "老郭",
        "occupation": "前端",
        "tech_stack": "TypeScript",
    }


async def test_patch_empty_string_deletes_field(api_client, clean):
    """传空串 = 删除这个字段（表单里"清空输入框"要能生效）。"""
    await api_client.patch("/api/profile", json={"nickname": "老郭", "occupation": "前端"})
    saved = await api_client.patch("/api/profile", json={"nickname": ""})
    assert saved.json()["explicit"] == {"occupation": "前端"}


async def test_patch_unknown_key_is_rejected(api_client, clean):
    """
    未知键 422，不是静默忽略。

    忽略会让前端拼错字段名时"保存成功但没生效" —— 接口一直 200，
    用户反复保存都改不动，这类问题几乎不可能从反馈里定位。
    """
    response = await api_client.patch("/api/profile", json={"hobby": "钓鱼"})
    assert response.status_code == 422


async def test_patch_too_long_is_rejected(api_client, clean):
    response = await api_client.patch("/api/profile", json={"occupation": "x" * 200})
    assert response.status_code == 422
    assert "最长" in response.json()["detail"]


# ============================================================
# 二、跨用户隔离
# ============================================================


async def test_explicit_isolated_between_users(api_client, user_client, clean):
    await api_client.patch("/api/profile", json={"nickname": "管理员专属称呼"})
    other = (await user_client.get("/api/profile")).json()
    assert other["explicit"] == {}


async def test_facts_isolated_between_users(api_client, user_client, normal_user, clean):
    await profile_service.add_facts(
        normal_user.id, [{"kind": "fact", "text": "普通用户的秘密事实", "confidence": 0.9}]
    )

    # A（管理员）看不到 B 的事实
    admin_view = (await api_client.get("/api/profile")).json()
    assert admin_view["facts"] == []

    # B 自己看得到
    user_view = (await user_client.get("/api/profile")).json()
    assert [f["text"] for f in user_view["facts"]] == ["普通用户的秘密事实"]


async def test_delete_own_fact(api_client, admin_user, clean):
    await profile_service.add_facts(
        admin_user.id, [{"kind": "preference", "text": "偏好 TypeScript"}]
    )
    facts = (await api_client.get("/api/profile")).json()["facts"]
    assert len(facts) == 1

    response = await api_client.delete(f"/api/profile/facts/{facts[0]['id']}")
    assert response.status_code == 204
    assert (await api_client.get("/api/profile")).json()["facts"] == []


async def test_delete_others_fact_returns_404(api_client, user_client, admin_user, clean):
    """
    删别人的事实 → **404 而不是 403**。

    403 等于承认"这个 id 存在，只是你没权限" —— 拿它就能枚举出别人有多少条画像。
    更重要的是：删完之后要确认**对方的数据还在**（免得"返回 404 但其实删掉了"）。
    """
    await profile_service.add_facts(
        admin_user.id, [{"kind": "fact", "text": "管理员的事实"}]
    )
    victim_id = (await api_client.get("/api/profile")).json()["facts"][0]["id"]

    response = await user_client.delete(f"/api/profile/facts/{victim_id}")
    assert response.status_code == 404

    still_there = (await api_client.get("/api/profile")).json()["facts"]
    assert [f["id"] for f in still_there] == [victim_id]


async def test_delete_missing_fact_returns_404(api_client, clean):
    assert (await api_client.delete("/api/profile/facts/99999999")).status_code == 404


# ============================================================
# 三、注入链路（跨会话可见性）
# ============================================================


async def test_render_for_prompt_reads_only_own_profile(admin_user, normal_user, clean):
    """
    `render_for_prompt` 是对话链路唯一的注入入口 —— 它必须按 owner 隔离。

    这条测的是"换一个会话还成立"的另一面：同一个人的不同会话都会读到它，
    而**别人的会话一个字都读不到**。
    """
    await profile_service.update_explicit(admin_user.id, {"nickname": "老郭"})

    mine = await profile_service.render_for_prompt(admin_user.id, "帮我看看这段代码")
    theirs = await profile_service.render_for_prompt(normal_user.id, "帮我看看这段代码")

    assert mine is not None and "老郭" in mine
    assert theirs is None

    # 没有归属（评测 / 脚本调用）→ 不注入，而不是报错
    assert await profile_service.render_for_prompt(None, "问题") is None


async def test_profile_survives_across_sessions(admin_user, clean):
    """
    **跨会话**这件事本身：画像不挂在 session 上，所以随便传什么 query 都能读到，
    与会话 id 无关。

    （与 `sessions.summary` 的对比正是这个功能的立项理由 ——
    摘要在 A 会话生成的，B 会话读不到；画像在 A 会话产生的，B 会话照样带上。）
    """
    await profile_service.add_facts(
        admin_user.id, [{"kind": "goal", "text": "正在准备 React 面试"}], source_session_id=None
    )
    block = await profile_service.render_for_prompt(admin_user.id, "React 的 hook 怎么写")
    assert block is not None
    assert "正在准备 React 面试" in block


async def test_fact_dedup_on_repeat(admin_user, clean):
    """同一条事实重复写入只留一条（规范化后哈希相同）。"""
    first = await profile_service.add_facts(
        admin_user.id, [{"kind": "fact", "text": "是前端工程师"}]
    )
    second = await profile_service.add_facts(
        admin_user.id, [{"kind": "fact", "text": "是前端工程师。"}]  # 只差句号
    )
    assert first == 1
    assert second == 0
    assert len((await profile_service.list_facts(admin_user.id))) == 1
