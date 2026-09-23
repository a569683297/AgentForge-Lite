"""
D14 补丁：messages.tool_calls 从 json（含 JSON null 脏值）修成 jsonb（SQL NULL）
==============================================================================
两个问题一起修：

① **类型不符**：PRD §10 定义的是 `JSONB`，库里实际建成 `json`。
② **值语义错**：SQLAlchemy 的 `JSON`/`JSONB` 默认 `none_as_null=False` ——
   传 Python `None` 时写进去的是 **JSON 字面量 `null`**，而不是 SQL NULL。实测：
       SELECT (tool_calls IS NULL) FROM messages WHERE role='user'  →  f
   于是 `WHERE tool_calls IS NULL` 永远查不到行 —— 所有"靠有没有 tool_calls
   判断消息类型"的逻辑全部失效。**D15 的 PG 回填正要靠它筛消息**，所以必须先修。

为什么用 ALTER 而不是 DROP 重建（对比 D12 的选择）：
   这次要改的是「列类型 + 该列的值形态」，不涉及加列/加外键/加表，
   ALTER 足够且不丢数据；D12 那次是加外键、换表结构，才只能重建。
   工程判断的依据是**变更的性质**，不是"上次怎么做的"。

执行（必须在项目根目录）：
    uv run python -m scripts.d14_fix_tool_calls
"""

import asyncio

from sqlalchemy import text

import app.models  # noqa: F401  导入即把所有模型注册到 Base.metadata
from app.core.db import engine
from app.core.logging import logger

_COL_TYPE_SQL = text(
    "SELECT data_type FROM information_schema.columns "
    "WHERE table_name = 'messages' AND column_name = 'tool_calls'"
)
# JSON 字面量 null 的文本形态就是 'null'（与 SQL NULL 的区别：后者 IS NULL 为真）
_JSON_NULL_COUNT_SQL = text(
    "SELECT count(*) FROM messages WHERE tool_calls::text = 'null'"
)


async def main() -> None:
    async with engine.begin() as conn:
        # ---- 迁移前：看清现状（既是证据，也是幂等判断的依据）----
        col_type = (await conn.execute(_COL_TYPE_SQL)).scalar()
        if col_type is None:
            raise RuntimeError("messages.tool_calls 列不存在，先跑 init_db / d12_migrate")

        total = (await conn.execute(text("SELECT count(*) FROM messages"))).scalar() or 0
        json_null = (await conn.execute(_JSON_NULL_COUNT_SQL)).scalar() or 0
        real_calls = (
            await conn.execute(
                text("SELECT count(*) FROM messages WHERE tool_calls::text LIKE '[%'")
            )
        ).scalar() or 0

        print(
            f"迁移前：列类型={col_type}  总行数={total}  "
            f"JSON null 脏值={json_null}  真工具调用={real_calls}"
        )

        if col_type == "jsonb" and json_null == 0:
            print("✅ 列类型已是 jsonb 且无脏值 —— 无需修复（脚本可重复运行）")
            return

        logger.info("① 清洗脏值：JSON 字面量 null → SQL NULL")
        cleaned = (
            await conn.execute(
                text("UPDATE messages SET tool_calls = NULL WHERE tool_calls::text = 'null'")
            )
        ).rowcount
        print(f"   清洗 {cleaned} 行")

        logger.info("② 列类型 json → jsonb")
        if col_type != "jsonb":
            await conn.execute(
                text(
                    "ALTER TABLE messages ALTER COLUMN tool_calls "
                    "TYPE jsonb USING tool_calls::jsonb"
                )
            )

        logger.info("③ 结构自检：确认列类型真的变成 jsonb")
        new_type = (await conn.execute(_COL_TYPE_SQL)).scalar()
        if new_type != "jsonb":
            # 抛异常 → 整个事务回滚，不留半成品结构（承接 D12 的教训）
            raise RuntimeError(f"结构自检失败：期望 jsonb，实际 {new_type}")

        logger.info("④ 数据自检：确认 JSON null 无残留，且真工具调用没被洗掉")
        left = (await conn.execute(_JSON_NULL_COUNT_SQL)).scalar() or 0
        kept = (
            await conn.execute(
                text("SELECT count(*) FROM messages WHERE tool_calls IS NOT NULL")
            )
        ).scalar() or 0
        if left:
            raise RuntimeError(f"数据自检失败：仍有 {left} 行是 JSON null")
        if kept != real_calls:
            raise RuntimeError(
                f"数据自检失败：清洗动作误伤了真工具调用"
                f"（清洗前 {real_calls} 行 → 现在 {kept} 行）"
            )

    print()
    print(f"✅ 修复完成：json → jsonb，清洗 {cleaned} 行 JSON null")
    print(f"✅ 结构自检通过：tool_calls 列类型 = jsonb")
    print(f"✅ 数据自检通过：真工具调用 {kept} 行原样保留，JSON null 残留 0")
    print("   说明：此后新写入的消息，非工具消息的 tool_calls 会是真正的 SQL NULL")


if __name__ == "__main__":
    asyncio.run(main())
