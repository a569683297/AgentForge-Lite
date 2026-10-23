"""
用户画像接口
============
    GET    /api/profile                 读画像（显式字段 + 隐式事实 + 字段定义）
    PATCH  /api/profile                 改显式字段（PATCH 语义：没传的保持不变，传空串 = 删除）
    DELETE /api/profile/facts/{id}      删一条隐式事实

权限：**全部声明权限点（profile.read / profile.write）**，且查询条件一律带 `user.id`。
没有"管理员看别人画像"的接口 —— 画像是最私密的一类数据，
"管理员能看"这个能力本身就不该存在（真要排障，走数据库而不是接口）。

为什么"删除"要单独一个接口、而不是做成 PATCH 事实列表：
  事实是**模型猜的**，用户唯一该有的操作是"这条不对，删掉"。
  让他编辑会引出"改完之后算显式的还是隐式的"这个无解问题 ——
  用户想说的事实，让他填到显式字段里；显式字段才是他"说了算"的地方。
  这条边界让两张表的语义始终清楚：**显式的用户负责，隐式的模型负责、用户可否决。**
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.permissions import PERM_PROFILE_READ, PERM_PROFILE_WRITE
from app.deps import require_permission
from app.models.user import User
from app.models.user_profile import EXPLICIT_FIELDS, FACT_KINDS
from app.schemas.profile import (
    ProfileExplicitPatch,
    ProfileFactOut,
    ProfileFieldOut,
    ProfileOut,
    ProfileUpdateOut,
)
from app.services import profile_service

router = APIRouter(prefix="/profile", tags=["profile"])


def _fields_payload() -> list[ProfileFieldOut]:
    return [
        ProfileFieldOut(key=key, label=label, max_length=max_len, hint=hint)
        for key, label, max_len, hint in EXPLICIT_FIELDS
    ]


def _fact_payload(fact: profile_service.FactItem) -> ProfileFactOut:
    return ProfileFactOut(
        id=fact.id,
        kind=fact.kind,
        text=fact.text,
        confidence=fact.confidence,
        source_session_id=str(fact.source_session_id) if fact.source_session_id else None,
        created_at=fact.created_at,
    )


@router.get("", response_model=ProfileOut)
async def get_profile(
    user: Annotated[User, Depends(require_permission(PERM_PROFILE_READ))],
) -> ProfileOut:
    """
    读当前用户的画像。

    ⚠ 这里用的是**不吞异常**的读取路径（`list_facts`），不是对话链路那条 fail-open 的
      `get_profile`。区别在于场景：页面拿不到数据必须报错，
      否则会显示"你还没有画像"—— 而真实原因可能是库挂了。**空态与故障必须可区分。**
    """
    explicit = (await profile_service.get_profile(user.id)).explicit
    facts = await profile_service.list_facts(user.id)
    return ProfileOut(
        explicit=explicit,
        facts=[_fact_payload(f) for f in facts],
        fields=_fields_payload(),
        fact_kinds=list(FACT_KINDS),
    )


@router.patch("", response_model=ProfileUpdateOut)
async def patch_profile(body: ProfileExplicitPatch, user: Annotated[User, Depends(require_permission(PERM_PROFILE_WRITE))]) -> ProfileUpdateOut:
    """
    改显式字段。

    合并逻辑放在**接口层**（而不是 service）：
      `update_explicit` 的语义是"整体替换"（一个明确、好测的契约）；
      "没传的保持原值"是 HTTP PATCH 的语义，属于协议层。
      把两者混在一起，service 就变成"有时替换有时合并"，那种函数没人能一眼看懂。
    """
    provided = body.model_dump(exclude_unset=True)      # 只含用户真的提交了的键
    current = (await profile_service.get_profile(user.id)).explicit
    merged = {**current, **{key: (value or "") for key, value in provided.items()}}

    try:
        saved = await profile_service.update_explicit(user.id, merged)
    except profile_service.ProfileValidationError as e:
        # 422 而不是 400：这是"字段值不合法"，与请求体结构错误同族。
        # ⚠ 用 `HTTP_422_UNPROCESSABLE_CONTENT` 而不是 `..._ENTITY`：
        #   Starlette 1.6 起后者是 deprecated 别名，用它会在**每次请求这条分支**时打
        #   DeprecationWarning —— 而 "422 只在用户填错时出现"，所以这条警告会零星地
        #   出现在日志里，看起来像随机噪音。常量名跟着上游走即可。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e
    return ProfileUpdateOut(explicit=saved)


@router.delete("/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fact(fact_id: int, user: Annotated[User, Depends(require_permission(PERM_PROFILE_WRITE))]) -> Response:
    """
    删一条事实。

    **不存在与不属于自己都返回 404**（不返回 403）：
      403 等于承认"这个 id 存在，只是你没权限"——
      攻击者用它就能枚举出别人有多少条画像。与文档/会话的越权处理保持一致。
    """
    ok = await profile_service.delete_fact(user.id, fact_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="这条画像不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
