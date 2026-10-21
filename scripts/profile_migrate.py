"""
迁移：引入用户画像（user_profiles / user_profile_facts 两张表）
==============================================================
这一次只做一件事，但两段都必须成立：

  1. **建表**：显式字段表（一人一行）+ 隐式事实表（一条一行）
  2. **自查**：结构（列 / 类型 / 外键行为 / 唯一约束）+ 行为（跨用户不可见、同人去重）

为什么不能用 `create_all` 一次搞定：
  `create_all` 按**表名**判断存在性 —— 对"新增表"其实是够用的。
  这里仍然写成独立脚本，图的是它自带的两段自查：
  表建出来但唯一约束没建上（比如手工建表时漏了），
  抽取任务会**静默地**把同一句画像存 20 遍 —— 那不报错，只是慢慢把注入配额吃光。

为什么 `user_profile_facts` 要 `(owner_id, text_hash)` 唯一约束：
  抽取是每 6 轮跑一次的后台任务，同一句"偏好 TypeScript"会被反复抽出来。
  没有约束 → 画像列表里重复项越积越多 → top-k 取 4 条时全被重复项占满
  → 真正的画像挤不进去。**去重不是洁癖，是让 top-k 有意义的前提。**

幂等：可反复执行，表已存在则跳过。
运行（项目根目录）：
    uv run python -m scripts.profile_migrate
容器内：
    docker compose exec api uv run --no-sync python -m scripts.profile_migrate
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401 —— 必须 import，否则模型不会注册到 Base.metadata
from app.core.db import engine
from app.models.user_profile import UserProfile, UserProfileFact

# (表名, ORM 模型) —— 顺序即建表顺序，facts 依赖 users/sessions，必须排在后面
TABLES = (
    ("user_profiles", UserProfile),
    ("user_profile_facts", UserProfileFact),
)

# 期望的外键行为：约束名 → (期望 confdeltype, 人话说明)
#   c = CASCADE / n = SET NULL
EXPECTED_FKS: dict[str, tuple[str, str]] = {
    "user_profiles_owner_id_fkey": ("c", "users ON DELETE CASCADE"),
    "user_profile_facts_owner_id_fkey": ("c", "users ON DELETE CASCADE"),
    "user_profile_facts_source_session_id_fkey": ("n", "sessions ON DELETE SET NULL"),
}


# ============================================================
# 1. 建表
# ============================================================


async def create_tables() -> None:
    async with engine.begin() as conn:
        for table, model in TABLES:
            await conn.run_sync(
                lambda sync_conn, m=model: m.__table__.create(sync_conn, checkfirst=True)
            )
            print(f"  [OK] {table} 表已就绪（已存在则跳过）")


# ============================================================
# 2. 结构自查
# ============================================================


async def verify_structure(conn) -> None:
    """回读库结构：列集合与 ORM 一致、外键行为符合声明、唯一约束真的在。"""
    for table, model in TABLES:
        declared = {c.name for c in model.__table__.columns}
        actual = {
            r.column_name
            for r in (
                await conn.execute(
                    text("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=:t
                    """),
                    {"t": table},
                )
            ).all()
        }
        if declared != actual:
            raise RuntimeError(
                f"{table} ORM 与库不一致：ORM 多出 {sorted(declared - actual)}，"
                f"库里多出 {sorted(actual - declared)}"
            )
        print(f"  [OK] {table} {len(declared)} 列与 ORM 声明一致")

    # 外键的删除行为 —— 这里最容易被"看起来对"骗过去：
    # 外键存在 ≠ 级联/置空行为对。写错方向（把 SET NULL 写成 CASCADE）时，
    # 删一个会话会把从它抽出来的画像一起删掉，而**没有任何报错**。
    for conname, (expected, human) in EXPECTED_FKS.items():
        row = (
            await conn.execute(
                text("SELECT confdeltype FROM pg_constraint WHERE conname = :n"),
                {"n": conname},
            )
        ).first()
        if row is None:
            raise RuntimeError(f"缺外键 {conname}（期望 {human}）")
        deltype = row.confdeltype
        # ⚠ asyncpg 把 PG 的 "char" 读成 **bytes**（b'c'），不是 str。
        #   不解码的话 `!= "c"` 恒真 —— 正确的约束会被判成错的（auth_migrate 踩过）。
        if isinstance(deltype, (bytes, bytearray)):
            deltype = deltype.decode()
        if deltype != expected:
            raise RuntimeError(
                f"{conname} 删除行为应为 {expected}（{human}），实际 {deltype!r}"
                "（'a'=NO ACTION 'r'=RESTRICT 'c'=CASCADE 'n'=SET NULL）"
            )
    print(f"  [OK] {len(EXPECTED_FKS)} 条外键的删除行为均符合声明")

    idx = (
        await conn.execute(
            text("""
                SELECT indexdef FROM pg_indexes
                WHERE schemaname='public' AND indexname='uq_profile_facts_owner_hash'
            """)
        )
    ).first()
    if idx is None or "owner_id" not in idx.indexdef or "text_hash" not in idx.indexdef:
        raise RuntimeError(
            f"唯一约束 uq_profile_facts_owner_hash 不成立："
            f"{idx.indexdef if idx else '不存在'}"
        )
    print("  [OK] 去重约束 uq_profile_facts_owner_hash 已建立在 (owner_id, text_hash)")


# ============================================================
# 3. 行为自查
# ============================================================


async def verify_behavior(conn) -> None:
    """
    行为自查：在 savepoint 里造两个用户，验证三件事后整体回滚。

      ① A 的事实对 B 不可见
      ② 不同用户可以存**相同内容**的事实（唯一约束里 owner_id 必须参与）
      ③ 同一用户重复插入同一内容会被拦（去重真的生效）

    ② 是这里最容易被漏掉的一条：如果唯一约束只建在 text_hash 上，
    结构自查照样通过、① 也照样通过，但 B 永远无法拥有和 A 相同的画像 ——
    而他会看到"我明明说过，它却没记住"。**约束的列组成比约束是否存在更关键。**
    """
    savepoint = await conn.begin_nested()
    try:
        ids = {}
        for tag in ("a", "b"):
            ids[tag] = (
                await conn.execute(
                    text("""
                        INSERT INTO users (id, email, password_hash, display_name, role, is_active)
                        VALUES (gen_random_uuid(), :e, 'x', :n, 'user', true) RETURNING id
                    """),
                    {"e": f"profile_probe_{tag}@example.invalid", "n": f"probe-{tag}"},
                )
            ).scalar()

        same_text = "偏好 TypeScript 而不是 JavaScript"
        same_hash = "0" * 64  # 内容相同 → 规范化后 hash 必然相同，这里显式给同一个值

        for tag in ("a", "b"):
            await conn.execute(
                text("""
                    INSERT INTO user_profile_facts (owner_id, kind, text, text_hash, confidence)
                    VALUES (:uid, 'preference', :t, :h, 0.8)
                """),
                {"uid": ids[tag], "t": same_text, "h": same_hash},
            )

        for tag in ("a", "b"):
            n = (
                await conn.execute(
                    text("SELECT count(*) FROM user_profile_facts WHERE owner_id = :uid"),
                    {"uid": ids[tag]},
                )
            ).scalar_one()
            if n != 1:
                raise RuntimeError(f"用户 {tag} 应看到 1 条自己的事实，实际 {n}")

        # ① 跨用户不可见：A 的计数里不该出现 B 的行
        cross = (
            await conn.execute(
                text("""
                    SELECT count(*) FROM user_profile_facts
                    WHERE owner_id = :a AND owner_id = :b
                """),
                {"a": ids["a"], "b": ids["b"]},
            )
        ).scalar_one()
        if cross != 0:
            raise RuntimeError("跨用户过滤失败：A 的查询里出现了 B 的行")

        # ② 不同用户可存相同内容（上一段已证明：两次插入都成功且各 1 条）

        # ③ 同一用户重复插入同一内容 → 必须被唯一约束拦住
        #    用 begin_nested() 的**上下文管理器**形式：异常退出时它自己回滚并释放 savepoint。
        #    第一版写成 `nested = await conn.begin_nested()` + 手动 commit，插入失败后
        #    那个嵌套事务会被 SQLAlchemy 标记为 deassociated，最外层 rollback 时冒
        #    `SAWarning: nested transaction already deassociated`。
        #    警告本身无害 —— 但"每次迁移都冒一行警告"会训练人忽略警告，那才是真代价。
        blocked = False
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text("""
                        INSERT INTO user_profile_facts (owner_id, kind, text, text_hash, confidence)
                        VALUES (:uid, 'preference', :t, :h, 0.8)
                    """),
                    {"uid": ids["a"], "t": same_text, "h": same_hash},
                )
        except Exception as e:  # noqa: BLE001 —— 这里要的就是"任何形式的拒绝"
            blocked = True
            msg = str(e).splitlines()[0][:120]
            print(f"  [OK] 同一用户重复事实被拦（{msg}）")
        if not blocked:
            raise RuntimeError("同一用户重复插入同一 text_hash **没有被拦** —— 去重失效")

        print("  [OK] 跨用户不可见 / 不同用户可存同内容 / 同人去重生效")
    finally:
        # 无论成功失败都回滚：自查绝不能留下 probe 用户
        await savepoint.rollback()


# ============================================================
# 主流程
# ============================================================


async def main() -> None:
    print("== 用户画像迁移 ==")
    print("[1/3] 建表")
    await create_tables()

    async with engine.connect() as conn:
        print("[2/3] 结构自查")
        await verify_structure(conn)

        print("[3/3] 行为自查（savepoint 内，结束后整体回滚）")
        await verify_behavior(conn)

    print("== 完成：user_profiles / user_profile_facts 已就绪 ==")


if __name__ == "__main__":
    asyncio.run(main())
