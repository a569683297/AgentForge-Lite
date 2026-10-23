"""
管理员接口（用户管理 + 审计日志）
================================
    GET    /api/admin/users            用户列表（分页）
    PATCH  /api/admin/users/{id}       启用/禁用、改角色
    GET    /api/admin/audit            审计日志（按动作前缀 / 操作者过滤）
    GET    /api/admin/audit/actions    可选动作清单（前端筛选下拉用）

为什么这些接口**全部**依赖 `require_admin`（而不是每个里面再判断一次角色）：
  权限判断写在一个依赖里，就不可能"某个接口忘了判断"。放到函数体里判断，
  新增一个接口时漏写的概率是必然的（不是可能）。

三条安全上的硬规则（都在本文件里强制执行，不依赖前端）：

1. **不能禁用自己**
   管理员一怒之下把自己禁用，就再也没有人能改回来 —— 除非上宿主机改库
   （`scripts/auth_set_password.py` 只改密码，不禁用状态）。
   这是"自锁"事故，必须在接口层挡住。

2. **不能把最后一个管理员降级**
   与第 1 条同源：系统里没有 admin 之后，用户管理接口全部 403，
   整个平台就进入了"只能改库"的状态。

3. **改自己的角色也走第 2 条**
   "让另一个 admin 顶上，再把自己降级"是合法操作 —— 只要降级之后还剩至少一个。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.core.db import async_session_factory
from app.core.permissions import PERM_ADMIN_AUDIT, PERM_ADMIN_USERS
from app.deps import require_permission
from app.models.audit_log import (
    ACTION_USER_DISABLED,
    ACTION_USER_ENABLED,
    ACTION_USER_ROLE_CHANGED,
    ACTIONS,
)
from app.models.user import ROLE_ADMIN, User
from app.schemas.admin import (
    AdminUserPatch,
    AuditActionOut,
    AuditLogListOut,
    AuditLogOut,
)
from app.schemas.auth import UserOut
from app.services import audit_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_ip(request: Request) -> str:
    """
    取来源 IP（尽力而为）。

    ⚠ 本项目没有可信代理配置，所以 `X-Forwarded-For` 一律**不信任** ——
      任何人都能自己伪造这个头。直接用 `request.client.host`：
      在 nginx 反代后面它恒为网关地址（127.0.0.1），也就是说这个字段
      在当前部署形态下参考价值有限。这一点如实写在这里，不假装它精确。
    """
    return request.client.host if request.client else ""


def _user_out(user: User) -> dict:
    """
    ORM → dict（复用 `UserOut` 的字段定义，**绝不含 password_hash**）。

    走 `from_user` 而不是 `model_validate`：管理员看用户列表时，
    "这个人当前有哪些权限点"是有用信息（比如排查"为什么他看不到某页"），
    而 model_validate 只会给一个空列表。
    """
    return UserOut.from_user(user).model_dump(mode="json")


@router.get("/users", summary="用户列表")
async def list_users_api(
    admin: Annotated[User, Depends(require_permission(PERM_ADMIN_USERS))],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    keyword: str = Query("", description="按邮箱/显示名模糊搜索"),
) -> dict:
    """按创建时间倒序。`keyword` 为空时不过滤。"""
    stmt = select(User)
    if keyword.strip():
        pattern = f"%{keyword.strip()}%"
        # ilike：邮箱大小写不敏感地搜 —— 用户不会记得自己注册时是大写还是小写
        stmt = stmt.where(User.email.ilike(pattern) | User.display_name.ilike(pattern))

    async with async_session_factory() as session:
        total = (
            await session.scalar(select(func.count()).select_from(stmt.subquery()))
        ) or 0
        rows = (
            await session.scalars(
                stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()

    return {"total": total, "items": [_user_out(user) for user in rows]}


@router.patch("/users/{user_id}", summary="启用/禁用、改角色")
async def patch_user_api(
    user_id: uuid.UUID,
    payload: AdminUserPatch,
    admin: Annotated[User, Depends(require_permission(PERM_ADMIN_USERS))],
    request: Request,
) -> dict:
    """
    改一个账号的状态。

    校验顺序很重要：**先算清楚"改完之后还剩几个 admin"**，再决定要不要拒绝。
    反过来的话（先写库再检查）就得回滚，而回滚窗口里别人可能已经读到中间状态。
    """
    async with async_session_factory() as session:
        target = await session.get(User, user_id)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在"
            )

        # ---- 规则 1：不能禁用自己 ----
        if target.id == admin.id and payload.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能禁用当前登录的账号（会把自己锁在门外）",
            )

        # ---- 规则 2：不能把最后一个管理员降级 ----
        if payload.role is not None and target.role == ROLE_ADMIN and payload.role != ROLE_ADMIN:
            remaining = (
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.role == ROLE_ADMIN, User.id != target.id)
                )
            ) or 0
            if remaining == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="这是最后一个管理员，不能降级（先把别人提升为管理员）",
                )

        before = {"is_active": target.is_active, "role": target.role}
        if payload.is_active is not None:
            target.is_active = payload.is_active
        if payload.role is not None:
            target.role = payload.role
        await session.commit()
        await session.refresh(target)
        after = {"is_active": target.is_active, "role": target.role}

        # ---- 审计：一次变更一条记录（动作名按"实际改了什么"选）----
        ip = _client_ip(request)
        if before["is_active"] != after["is_active"]:
            await audit_service.record(
                action=ACTION_USER_ENABLED if after["is_active"] else ACTION_USER_DISABLED,
                actor_id=admin.id,
                actor_email=admin.email,
                target_type="user",
                target_id=target.id,
                detail={"email": target.email, "from": before["is_active"], "to": after["is_active"]},
                ip=ip,
            )
        if before["role"] != after["role"]:
            await audit_service.record(
                action=ACTION_USER_ROLE_CHANGED,
                actor_id=admin.id,
                actor_email=admin.email,
                target_type="user",
                target_id=target.id,
                detail={"email": target.email, "from": before["role"], "to": after["role"]},
                ip=ip,
            )

        return _user_out(target)


@router.get("/audit/actions", summary="可选的动作清单")
async def list_actions_api(
    admin: Annotated[User, Depends(require_permission(PERM_ADMIN_AUDIT))],
) -> AuditActionOut:
    """前端筛选下拉从这里取，不硬编码 —— 新增动作时不用改前端。"""
    return AuditActionOut(actions=list(ACTIONS))


@router.get("/audit", summary="审计日志")
async def list_audit_api(
    admin: Annotated[User, Depends(require_permission(PERM_ADMIN_AUDIT))],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: Annotated[str | None, Query(description="动作前缀，如 auth. / admin.")] = None,
    actor_id: Annotated[uuid.UUID | None, Query(description="只看某个人的操作")] = None,
) -> AuditLogListOut:
    """
    按时间倒序读审计日志。

    `total` 是**当前过滤条件下的总数**（前端分页要用），与 items 分开查。
    为了让 total 与 items 口径一致，两处用同样的过滤条件 —— 这类"总数和列表不一致"
    的 bug 通常就是两处各写了一遍 where。
    """
    items = await audit_service.list_logs(
        limit=limit, offset=offset, action_prefix=action or None, actor_id=actor_id
    )

    from app.models.audit_log import AuditLog

    stmt = select(func.count()).select_from(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    async with async_session_factory() as session:
        total = (await session.scalar(stmt)) or 0

    return AuditLogListOut(
        total=total, items=[AuditLogOut(**item) for item in items]
    )
