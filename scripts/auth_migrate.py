"""
迁移：引入账号体系（users 表 + 五张业务表的 owner_id + 引导管理员 + 存量归属回填）
================================================================================
这一次迁移要同时完成四件互相依赖的事，少任何一件都会留下一个洞：

  1. **建 users 表**（`Base.metadata` 里已声明，但库里没有）
  2. **给五张业务表加 `owner_id`** + 索引 + 外键
     sessions / documents / tool_servers / eval_runs / llm_models
  3. **去重唯一索引的收窄**：`uq_documents_content_hash`（全库唯一）
     → `uq_documents_owner_hash`（(owner_id, content_hash) 唯一）
  4. **引导管理员 + 存量数据归属回填**

为什么不能只用 `create_all`：
  它按**表名**判断存在性。五张业务表早就在库里，整张表会被跳过 ——
  新列永远加不上，而命令"成功"了。这是 D21 / messages.trace_id 踩过的同一个坑，
  第三次出现（说明"建表工具"和"改表需求"根本不是同一件事）。

关于第 3 条（这条如果漏了，隔离是假的）：
  旧的唯一索引在全库范围内判重。用户 B 上传一份 A 已传过的文件时，
  `begin_upload` 会命中 A 的那一行 → 直接复用 A 的文档 id、A 的切片、A 的引用。
  也就是说：**B 什么都没上传，却"拥有"了 A 的文档**。
  索引不收窄到人，后面所有 WHERE owner_id 的过滤都会被这个后门绕过。

关于第 4 条的密码：
  `AUTH_BOOTSTRAP_PASSWORD` 为空时**生成随机密码并只打印一次**，
  而不是设一个 `admin123` 之类的默认值 —— 默认密码是这个项目里最容易被
  带到线上、又最不可能被改的东西。打印在迁移输出里，抄走即可。

幂等：可反复执行。表/列/索引/约束已存在时跳过；已有用户时不再建引导账号。
运行（项目根目录）：
    uv run python -m scripts.auth_migrate
容器内：
    docker compose exec api uv run --no-sync python -m scripts.auth_migrate
"""

from __future__ import annotations

import asyncio
import secrets

from sqlalchemy import text

import app.models  # noqa: F401 —— 必须 import，否则模型不会注册到 Base.metadata
from app.config import settings
from app.core.db import engine
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.eval import EvalRun
from app.models.llm_model import LlmModel
from app.models.session import Session
from app.models.tool_server import ToolServer
from app.models.user import ROLE_ADMIN, User

# 需要加 owner_id 的表（顺序无关，但列出来便于核对）
OWNED_MODELS = (Session, Document, ToolServer, EvalRun, LlmModel)

OLD_HASH_INDEX = "uq_documents_content_hash"
NEW_HASH_INDEX = "uq_documents_owner_hash"


def _fk_name(table: str) -> str:
    """外键约束名。显式命名而不是让 PG 自动生成（`sessions_owner_id_fkey`）——
    自动名在重建表时会变，`DROP CONSTRAINT IF EXISTS` 就删不掉旧的了。"""
    return f"fk_{table}_owner"


# ============================================================
# 1. users 表
# ============================================================


async def create_users_table() -> None:
    async with engine.begin() as conn:
        # 只建这一张表（不 create_all）：其余表已存在，create_all 会整表跳过，
        # 而这里要的正是"只补缺的那张"。
        await conn.run_sync(lambda sync_conn: User.__table__.create(sync_conn, checkfirst=True))
        # 审计表与 users 一起建：它有一个外键指向 users.id，顺序上必须在 users 之后。
        # 为什么放在同一个迁移里而不是单独一个脚本：审计是账号体系的**配套物** ——
        # 没有审计的账号体系，管理员做过的每一件事都无从追查。
        await conn.run_sync(
            lambda sync_conn: AuditLog.__table__.create(sync_conn, checkfirst=True)
        )
    print("  [OK] users / audit_logs 表已就绪（已存在则跳过）")


# ============================================================
# 2. owner_id 列 + 索引 + 外键
# ============================================================


async def add_owner_columns() -> None:
    async with engine.begin() as conn:
        for model in OWNED_MODELS:
            table = model.__tablename__
            # nullable=True：迁移前的历史行还没有归属，先允许 NULL，
            # 回填完再靠应用层保证"新写入必有值"（不改成 NOT NULL，
            # 是因为将来若引入"公共资源"（如系统内置文档），也需要 NULL 表达"无主"）。
            await conn.execute(text(f"""
                ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS owner_id uuid
                REFERENCES users(id) ON DELETE CASCADE
            """))
            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS ix_{table}_owner_id ON {table} (owner_id)
            """))
            # 上面 ADD COLUMN 带的 REFERENCES 只在"列是新建的"时才生效；
            # 列已存在（重复执行）时不会补外键。所以这里显式再兜一次：
            await conn.execute(text(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = '{_fk_name(table)}'
                    ) THEN
                        ALTER TABLE {table}
                        ADD CONSTRAINT {_fk_name(table)}
                        FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE;
                    END IF;
                END $$;
            """))
            print(f"  [OK] {table}.owner_id + 索引 + 外键")


# ============================================================
# 3. 唯一索引收窄
# ============================================================


async def narrow_hash_index() -> None:
    async with engine.begin() as conn:
        exists_old = (await conn.execute(text(
            "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=:n"
        ), {"n": OLD_HASH_INDEX})).first()
        if exists_old:
            await conn.execute(text(f"DROP INDEX {OLD_HASH_INDEX}"))
            print(f"  [OK] 已删除全局唯一索引 {OLD_HASH_INDEX}")
        else:
            print(f"  [--] {OLD_HASH_INDEX} 不存在（已迁移过）")

        await conn.execute(text(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {NEW_HASH_INDEX}
            ON documents (owner_id, content_hash)
        """))
        print(f"  [OK] {NEW_HASH_INDEX} = UNIQUE(owner_id, content_hash)")


# ============================================================
# 4. 引导管理员 + 回填
# ============================================================


async def bootstrap_admin() -> tuple[str, str, str]:
    """
    保证库里至少有一个 admin，返回 (id, email, 明文密码或空串)。

    密码来源优先级：
      ① `AUTH_BOOTSTRAP_PASSWORD`（部署时显式指定，最可控）
      ② 随机生成，打印在迁移输出里（只此一次，之后只能重置）
    """
    from app.core.security import hash_password

    async with engine.begin() as conn:
        row = (await conn.execute(text(
            "SELECT id, email FROM users WHERE role = :r ORDER BY created_at LIMIT 1"
        ), {"r": ROLE_ADMIN})).first()

        if row is not None:
            print(f"  [--] 已有管理员 {row.email}，跳过创建")
            return str(row.id), row.email, ""

        plain = settings.auth_bootstrap_password or secrets.token_urlsafe(12)
        await conn.execute(text("""
            INSERT INTO users (id, email, password_hash, display_name, role, is_active)
            VALUES (gen_random_uuid(), :email, :pwd, :name, :role, true)
        """), {
            "email": settings.auth_bootstrap_email,
            "pwd": hash_password(plain),      # 生产成本因子 12
            "name": "管理员",
            "role": ROLE_ADMIN,
        })
        uid = (await conn.execute(text(
            "SELECT id FROM users WHERE email = :e"
        ), {"e": settings.auth_bootstrap_email})).scalar_one()

    print(f"  [OK] 已创建引导管理员 {settings.auth_bootstrap_email}")
    # ⚠ 密码必须**在这里立刻打印**，不能攒到最后汇总输出。
    #   第一版就是攒到最后，结果第 6 步自查报错、脚本中途退出 ——
    #   账号已经建好（密码是随机的、只有哈希落库），而明文再也不会出现。
    #   教训：**凭证类输出要在产生的那一刻就交给人**，脚本后半段随时可能失败。
    if settings.auth_bootstrap_password:
        print("       密码来自 AUTH_BOOTSTRAP_PASSWORD（.env 里可查）")
    else:
        print(f"       ⚠ 初始密码（只显示这一次）：{plain}")
    return str(uid), settings.auth_bootstrap_email, plain


async def backfill_owner(admin_id: str) -> None:
    """
    把所有 owner_id IS NULL 的历史行划给引导管理员。

    为什么划给 admin 而不是"平均分给谁"或"留空"：
      · 留空 → 这些数据在**任何人**的列表里都不出现，等于静默丢失（用户会以为文档没了）
      · 平均分 → 凭空造出一个错误的归属，比丢失更糟：它看起来是对的
      · 划给 admin → 唯一可解释的选择。原来这套数据是"无主的公共数据"，
        现在由管理员接管（管理员本来就负责维护知识库与评测语料）。
    """
    async with engine.begin() as conn:
        for model in OWNED_MODELS:
            table = model.__tablename__
            result = await conn.execute(text(f"""
                UPDATE {table} SET owner_id = :uid WHERE owner_id IS NULL
            """), {"uid": admin_id})
            print(f"  [OK] {table} 回填 {result.rowcount} 行 → 引导管理员")


# ============================================================
# 自查
# ============================================================


async def verify_structure(conn) -> None:
    """回读库结构：users 的列、五张表的 owner_id、索引、外键。"""
    cols = {
        r.column_name for r in (await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='users'
        """))).all()
    }
    expected = {c.name for c in User.__table__.columns}
    if cols != expected:
        raise RuntimeError(f"users 列不符：期望 {sorted(expected)}，实际 {sorted(cols)}")
    print(f"  [OK] users 表 {len(cols)} 列与 ORM 声明一致")

    audit_cols = {
        r.column_name for r in (await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='audit_logs'
        """))).all()
    }
    audit_expected = {c.name for c in AuditLog.__table__.columns}
    if audit_cols != audit_expected:
        raise RuntimeError(
            f"audit_logs 列不符：期望 {sorted(audit_expected)}，实际 {sorted(audit_cols)}"
        )
    print(f"  [OK] audit_logs 表 {len(audit_cols)} 列与 ORM 声明一致")

    for model in OWNED_MODELS:
        table = model.__tablename__
        row = (await conn.execute(text("""
            SELECT data_type, is_nullable FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t AND column_name='owner_id'
        """), {"t": table})).first()
        if row is None:
            raise RuntimeError(f"{table}.owner_id 不存在")
        if row.data_type != "uuid":
            raise RuntimeError(f"{table}.owner_id 类型应为 uuid，实际 {row.data_type}")
        if row.is_nullable != "YES":
            raise RuntimeError(f"{table}.owner_id 应允许 NULL（历史/系统资源）")

        fk = (await conn.execute(text("""
            SELECT confdeltype FROM pg_constraint WHERE conname = :n
        """), {"n": _fk_name(table)})).first()
        if fk is None:
            raise RuntimeError(f"{table} 缺外键 {_fk_name(table)}")
        # ⚠ asyncpg 把 PostgreSQL 的 "char" 类型读成 **bytes**（b'c'），不是 str。
        #   第一版写成 `fk.confdeltype != "c"`，于是永远不相等 —— 自查把一条正确的
        #   外键判成错的。这类"类型没对上导致判定恒假/恒真"的坑，比逻辑写错更难发现。
        deltype = fk.confdeltype
        if isinstance(deltype, (bytes, bytearray)):
            deltype = deltype.decode()
        if deltype != "c":      # c = CASCADE
            raise RuntimeError(
                f"{table} 外键删除行为应为 CASCADE，实际 {deltype!r}"
                "（'a'=NO ACTION 'r'=RESTRICT 'n'=SET NULL）"
            )
    print(f"  [OK] 五张业务表 owner_id 均为 uuid + FK ON DELETE CASCADE")

    idx = (await conn.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname='public' AND indexname=:n
    """), {"n": NEW_HASH_INDEX})).first()
    if idx is None or "owner_id" not in idx.indexdef or "content_hash" not in idx.indexdef:
        raise RuntimeError(f"索引 {NEW_HASH_INDEX} 不成立：{idx.indexdef if idx else '不存在'}")
    print(f"  [OK] 去重索引已收窄到 (owner_id, content_hash)")


async def verify_metadata_match(conn) -> None:
    """交叉核对：ORM 声明的列 == 库里的列（抓「改了模型忘了迁移」）。"""
    for model in OWNED_MODELS:
        table = model.__tablename__
        declared = {c.name for c in model.__table__.columns}
        actual = {
            r.column_name for r in (await conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:t
            """), {"t": table})).all()
        }
        if declared != actual:
            raise RuntimeError(
                f"{table} ORM 与库不一致：ORM 多出 {sorted(declared - actual)}，"
                f"库里多出 {sorted(actual - declared)}"
            )
    print(f"  [OK] 五张业务表 ORM 声明与库结构一致")


async def verify_behavior(conn) -> None:
    """
    行为自查：**两个"用户"的数据互不可见**。

    只验结构无法证明隔离成立 —— 列在、索引在，查询漏写一句 WHERE 就全白费。
    这里在 savepoint 里造两个临时用户各一条会话，用**和业务代码同一条**过滤条件
    去查，然后整体回滚。
    """
    savepoint = await conn.begin_nested()
    try:
        ids = {}
        for tag in ("a", "b"):
            ids[tag] = (await conn.execute(text("""
                INSERT INTO users (id, email, password_hash, display_name, role, is_active)
                VALUES (gen_random_uuid(), :e, 'x', :n, 'user', true) RETURNING id
            """), {"e": f"probe_{tag}@example.invalid", "n": f"probe-{tag}"})).scalar()

        for tag in ("a", "b"):
            await conn.execute(text("""
                INSERT INTO sessions (id, title, owner_id)
                VALUES (gen_random_uuid(), :t, :uid)
            """), {"t": f"probe-session-{tag}", "uid": ids[tag]})

        seen = {}
        for tag in ("a", "b"):
            seen[tag] = (await conn.execute(text("""
                SELECT title FROM sessions WHERE owner_id = :uid
            """), {"uid": ids[tag]})).scalars().all()

        if seen["a"] != ["probe-session-a"]:
            raise RuntimeError(f"A 的会话列表不对：{seen['a']}")
        if seen["b"] != ["probe-session-b"]:
            raise RuntimeError(f"B 的会话列表不对：{seen['b']}")
        print("  [OK] 归属过滤探针通过：A 只看到自己的会话，B 只看到自己的")

        # 去重索引必须**按人**判重：同 hash 不同 owner 可以共存
        await conn.execute(text("""
            INSERT INTO documents (id, filename, file_type, content_hash, status, chunk_count, owner_id)
            VALUES (gen_random_uuid(), 'probe.md', 'md', 'probe-hash-0001', 'ready', 1, :a),
                   (gen_random_uuid(), 'probe.md', 'md', 'probe-hash-0001', 'ready', 1, :b)
        """), {"a": ids["a"], "b": ids["b"]})
        print("  [OK] 同一文件两人各存一份：唯一索引按 (owner_id, content_hash) 判重，不互相顶掉")

        # 同一 owner 内重复仍必须被挡住
        try:
            async with conn.begin_nested():
                await conn.execute(text("""
                    INSERT INTO documents (id, filename, file_type, content_hash, status, chunk_count, owner_id)
                    VALUES (gen_random_uuid(), 'probe.md', 'md', 'probe-hash-0001', 'ready', 1, :a)
                """), {"a": ids["a"]})
            raise RuntimeError("同一 owner 重复插入未被唯一索引拦住 —— 去重失效")
        except Exception as exc:  # noqa: BLE001
            if "duplicate key" not in str(exc):
                raise
            print("  [OK] 同一 owner 内重复仍被拦住（幂等去重没有因为收窄而失效）")
    finally:
        await savepoint.rollback()

    left = (await conn.execute(text(
        "SELECT count(*) FROM users WHERE email LIKE 'probe_%@example.invalid'"
    ))).scalar_one()
    if left != 0:
        raise RuntimeError(f"探针数据残留 {left} 条，回滚没生效")
    print("  [OK] 探针已回滚，无残留")


async def main() -> None:
    print("=" * 74)
    print("迁移：账号体系（users + 五表 owner_id + 引导管理员 + 回填）")
    print("=" * 74)
    print("[1/7] 建 users / audit_logs 表")
    await create_users_table()
    print("[2/7] 五张业务表加 owner_id（列 + 索引 + 外键）")
    await add_owner_columns()
    print("[3/7] 文档去重唯一索引收窄到 (owner_id, content_hash)")
    await narrow_hash_index()
    print("[4/7] 引导管理员")
    admin_id, admin_email, plain = await bootstrap_admin()
    print("[5/7] 存量数据回填归属")
    await backfill_owner(admin_id)
    print("[6/7] 结构自查")
    async with engine.begin() as conn:
        await verify_structure(conn)
        await verify_metadata_match(conn)
    print("[7/7] 行为自查（两人互不可见 + 去重按人成立）")
    async with engine.begin() as conn:
        await verify_behavior(conn)

    await engine.dispose()
    print("=" * 74)
    print("迁移完成")
    print(f"  管理员账号：{admin_email}")
    if plain:
        print(f"  初始密码：  {plain}")
        print("  ⚠ 这串密码**只显示这一次**（库里只有哈希），请立刻登录并修改。")
    else:
        print("  密码：沿用已有账号，未改动")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
