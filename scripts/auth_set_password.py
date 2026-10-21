"""
运维脚本：直接改某个账号的密码（忘记管理员密码时的救援通道）
=============================================================
用法（项目根目录）：
    uv run python -m scripts.auth_set_password admin@agentforge.local
    # 交互式输入新密码（不回显，避免留在 shell history 里）
    # 或者：
    uv run python -m scripts.auth_set_password admin@agentforge.local --password 'xxx'
    # 或者从环境变量读（CI / 自动化）：
    NEW_PASSWORD='xxx' uv run python -m scripts.auth_set_password admin@agentforge.local --from-env

容器内：
    docker compose exec api uv run --no-sync python -m scripts.auth_set_password admin@agentforge.local

为什么需要它（而不是"再跑一次迁移"）：
  迁移脚本只在**库里没有任何 admin 时**才创建账号。只要引导管理员已经存在，
  它就会跳过 —— 那是对的（迁移不该悄悄重置密码），但于是一旦明文丢了就没有入口。
  忘记密码的正规通道是邮件重置，而"邮件都没配/进不去"时只能从宿主机进库改。
  所以这个脚本是**最后一级台阶**，不是日常工具。

它会做什么：
  ① 按邮箱找到用户（**不新建**，邮箱打错就报错，绝不静默创建一个新号）
  ② 用生产成本因子（12）重新哈希
  ③ 同时刷新 `password_changed_at` —— 于是该账号**所有已签发的 refresh token 立刻失效**
     （access token 仍有效到过期，这是无状态 JWT 的固有代价）
  ④ 打印账号现状，不打印密码（密码是你给的，回显它只会在终端留痕）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select, text

import app.models  # noqa: F401 —— 注册模型
from app.core.db import async_session_factory, engine
from app.core.security import hash_password
from app.models.user import User


def _read_password(args: argparse.Namespace) -> str:
    if args.password:
        return args.password
    if args.from_env:
        value = os.environ.get("NEW_PASSWORD", "")
        if not value:
            sys.exit("环境变量 NEW_PASSWORD 为空")
        return value
    # 交互式：不回显、二次确认。打错一个字符就锁死自己的场景不值得冒
    first = getpass.getpass("新密码（不回显）：")
    second = getpass.getpass("再输一次：")
    if first != second:
        sys.exit("两次输入不一致，已中止")
    return first


async def main() -> None:
    parser = argparse.ArgumentParser(description="直接重置某个账号的密码")
    parser.add_argument("email", help="账号邮箱（必须已存在）")
    parser.add_argument("--password", default="", help="新密码（明文会进 shell history，慎用）")
    parser.add_argument(
        "--from-env", action="store_true", help="从环境变量 NEW_PASSWORD 读取（适合自动化）"
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    password = _read_password(args)

    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            # 刻意不创建：这条路径的典型误用是"邮箱打错"，静默建号会让人以为改成功了
            sys.exit(f"账号不存在：{email}")

        user.password_hash = hash_password(password)          # 成本因子 12
        # 与 auth_service.change_password 保持一致：改密必须让旧 refresh 失效
        await session.execute(
            text("UPDATE users SET password_changed_at = now() WHERE id = :uid"),
            {"uid": user.id},
        )
        await session.commit()
        await session.refresh(user)
        print(f"[OK] 已重置密码 {user.email}（role={user.role} active={user.is_active}）")
        print("     该账号此前签发的 refresh token 已全部失效；access token 最长 2 小时后过期。")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
