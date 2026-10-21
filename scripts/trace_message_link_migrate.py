"""
迁移：messages 增加 trace_id（把「一次提问」和「一条 trace」连起来）
====================================================================
为什么需要这一列 —— 这是这次改造的**唯一支点**，不是顺手加个字段：

  Trace 页要按「提问」为单位展示时间线，就必须知道每次提问问了什么。
  三条路实测下来只剩这一条：

    · v2 `/api/public/v2/observations`（Trace 页现用）**不返回 input**
      —— 实测键集：id / traceId / parentObservationId / name / type /
      startTime / endTime / latency / level / sessionId / modelId，
      没有 input、没有 output、也没有 metadata。拿不到提问文本。
    · v1 `/api/public/traces/{id}` **带** input（实测
      `{"user_input": "距离2027年10月1日还有几天？..."}`），但它
      2026-11-16 下线，而且一次只能查一条（20 条 trace = 20 次请求）。
      把功能建在它上面 = 27 天后必然失效。
    · **自己的 messages 表本来就有提问原文** → 只要存下 trace id 就能对上。

为什么加在 messages 而不是 sessions：
  一次提问 = 一条 trace = 一条 user 消息。sessions 是一整段会话，
  一个 session 有 N 次提问、N 条 trace，挂不到具体哪一条上。

为什么只有 role=user 的行有值：
  同一轮的 assistant / tool 消息重复挂同一个 id，会让
  `WHERE trace_id IN (...)` JOIN 出多行，把"一次提问"变成"三条记录"，
  上层还得再按 role 去重一次。让 user 消息独占，语义最干净。

为什么不回填历史数据：
  历史消息写入时根本没有取过 trace id，无从回填。
  老 trace 在页面上会显示「该次提问早于 trace 记录」，这是**如实呈现**，
  而不是编一个近似值——那会让"哪次是猜的"永远说不清。

幂等：可反复执行，列与索引已存在时不会重复创建。
运行（项目根目录）：
    uv run python -m scripts.trace_message_link_migrate
容器内：
    docker compose exec api uv run --no-sync python -m scripts.trace_message_link_migrate
"""

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401 —— 必须 import，否则模型不会注册到 Base.metadata
from app.core.db import engine
from app.models.message import Message

TABLE = Message.__tablename__
COLUMN = "trace_id"
INDEX = "ix_messages_trace_id"

# 期望类型：varchar(32)。
#   ⚠ 与 tool_server_migrate 同样的坑：PG 的 information_schema 里 varchar 的
#     data_type 是 "character varying"，短名在 udt_name。两列都要对，只对一个是自欺欺人。
EXPECTED = ("character varying", "varchar")


async def add_column() -> None:
    """
    加列。用 ADD COLUMN IF NOT EXISTS 而不是 create_all。

    为什么不用 `Base.metadata.create_all(checkfirst=True)`：
    它只按**表名**判断 —— messages 表早就存在，整张表会被跳过，
    新列永远加不上，而命令"成功"了。这正是 D21 记下的那类
    "语句成功、结果是错的"。
    """
    async with engine.begin() as conn:
        await conn.execute(text(f"""
            ALTER TABLE {TABLE}
            ADD COLUMN IF NOT EXISTS {COLUMN} varchar(32)
        """))
        print(f"  [OK] ALTER TABLE {TABLE} ADD COLUMN {COLUMN}（已存在则跳过）")

        # 索引：Trace 页是 `WHERE trace_id = ANY($1)` 的批量查，
        # 没有索引就是全表扫（messages 会随对话量线性增长）。
        await conn.execute(text(f"""
            CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} ({COLUMN})
        """))
        print(f"  [OK] CREATE INDEX {INDEX}（已存在则跳过）")


async def verify_column(conn) -> None:
    """回读库结构：列真的在、类型真的是 varchar(32)。"""
    row = (await conn.execute(text("""
        SELECT data_type, udt_name, character_maximum_length, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :t AND column_name = :c
    """), {"t": TABLE, "c": COLUMN})).first()

    if row is None:
        raise RuntimeError(f"{TABLE}.{COLUMN} 不存在（ALTER 没生效）")
    if (row.data_type, row.udt_name) != EXPECTED:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} 类型不符：期望 {EXPECTED}，实际 "
            f"{(row.data_type, row.udt_name)}"
        )
    if row.character_maximum_length != 32:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} 长度不符：期望 32，实际 {row.character_maximum_length}"
        )
    if row.is_nullable != "YES":
        raise RuntimeError(
            f"{TABLE}.{COLUMN} 必须是 nullable —— 历史消息与 assistant 消息都没有 trace id"
        )
    print(f"  [OK] {TABLE}.{COLUMN} = {row.data_type}({row.character_maximum_length}), nullable")


async def verify_index(conn) -> None:
    """索引真的建在 trace_id 上（名字对、列不对也是白搭）。"""
    rows = (await conn.execute(text("""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = :t
    """), {"t": TABLE})).all()
    hit = [r.indexdef for r in rows if r.indexname == INDEX]
    if not hit:
        raise RuntimeError(
            f"{TABLE} 没有索引 {INDEX}，现有：{[r.indexname for r in rows]}"
        )
    if COLUMN not in hit[0]:
        raise RuntimeError(f"索引 {INDEX} 建在了别的列上：{hit[0]}")
    print(f"  [OK] 索引就位：{hit[0]}")


async def verify_metadata_match(conn) -> None:
    """交叉核对：ORM 声明的列 == 库里的列（抓「改了模型忘了迁移」）。"""
    declared = {c.name for c in Message.__table__.columns}
    actual = {
        r.column_name for r in (await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t
        """), {"t": TABLE})).all()
    }
    if declared != actual:
        raise RuntimeError(
            f"{TABLE} ORM 与库不一致：ORM 多出 {sorted(declared - actual)}，"
            f"库里多出 {sorted(actual - declared)}"
        )
    print(f"  [OK] {TABLE} ORM 声明与库结构一致（{len(declared)} 列）")


async def verify_behavior(conn) -> None:
    """
    行为自查：带 trace_id 的消息真的能被批量查回来。

    结构自查只能证明"有一列"，证明不了"写进去、按 IN 查得出来"。
    探针在 savepoint 里跑，**结束即回滚**，不留数据。
    """
    savepoint = await conn.begin_nested()
    try:
        # 用一个真实存在的会话；没有就造一个（同样在 savepoint 内，会回滚）
        sid = (await conn.execute(text("SELECT id FROM sessions LIMIT 1"))).scalar()
        if sid is None:
            sid = (await conn.execute(text("""
                INSERT INTO sessions (id, title) VALUES (gen_random_uuid(), 'migrate_probe')
                RETURNING id
            """))).scalar()

        await conn.execute(text(f"""
            INSERT INTO {TABLE} (session_id, role, content, {COLUMN})
            VALUES (:sid, 'user', '迁移探针：年假有几天？', 'probe_trace_0001'),
                   (:sid, 'assistant', '探针回答', NULL)
        """), {"sid": sid})

        got = (await conn.execute(text(f"""
            SELECT content FROM {TABLE}
            WHERE {COLUMN} = ANY(:ids) AND role = 'user'
        """), {"ids": ["probe_trace_0001", "probe_trace_不存在"]})).all()

        if len(got) != 1:
            raise RuntimeError(f"按 trace_id 批量查期望 1 行，实际 {len(got)} 行")
        if got[0].content != "迁移探针：年假有几天？":
            raise RuntimeError(f"查回来的内容不对：{got[0].content!r}")
        print("  [OK] 写入 + 按 trace_id 批量查通（assistant 行不带 id，未被误命中）")
    finally:
        await savepoint.rollback()

    left = (await conn.execute(text(
        f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} = 'probe_trace_0001'"
    ))).scalar_one()
    if left != 0:
        raise RuntimeError(f"探针数据残留 {left} 条，回滚没生效")
    print("  [OK] 探针已回滚，无残留")


async def main() -> None:
    print("=" * 74)
    print("迁移：messages.trace_id（把一次提问与一条 trace 关联起来）")
    print("=" * 74)
    print("[1/4] 加列 + 建索引")
    await add_column()
    print("[2/4] 回读库结构自查")
    async with engine.begin() as conn:
        await verify_column(conn)
        await verify_index(conn)
    print("[3/4] ORM 声明与库结构交叉核对")
    async with engine.begin() as conn:
        await verify_metadata_match(conn)
    print("[4/4] 行为自查（写入 + 批量查 + 回滚）")
    async with engine.begin() as conn:
        await verify_behavior(conn)

    await engine.dispose()
    print("=" * 74)
    print("迁移完成")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
