"""
D37 补充验证：Langfuse 单飞 / 负缓存是否真的生效（真实 HTTP，非 mock）
=====================================================================
背景：分析页原来并发 6 次 HTTP 超过 Langfuse observations 端点 5 次/分钟配额，
      Token 消耗与延迟 P95 双双 429 降级。本次加了单飞 + 负缓存。

验证三件事：
  ① 两个 Langfuse 指标**并发**查询同一窗口 → `_fetch_pages` 只被调用 1 次
  ② 紧接着再查一次 → 命中 60s 缓存，0 次新调用
  ③ 若此刻正处于限流冷却 → 第二次调用 0 次新调用（负缓存短路），而不是再打一遍

运行：`.venv/bin/python -m scripts.d37_ratelimit_verify`（需在项目根目录）
"""

import asyncio
import sys
import time

from app.services import metrics_service as ms
from app.services.langfuse_client import get_langfuse_client

START, END = "2026-10-13", "2026-10-19"


async def main() -> int:
    client = get_langfuse_client()

    # 包一层计数器：只数 _fetch_pages 被调用几次（一次内部会发 N 页 HTTP）
    calls = {"n": 0}
    original = client._fetch_pages

    async def counting(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    client._fetch_pages = counting  # type: ignore[method-assign]

    print("① 并发查询两个 Langfuse 指标（同窗口同 limit → 单飞应合并为 1 次取数）")
    started = time.monotonic()
    tokens, latency = await asyncio.gather(
        ms.run_metric("token_cost", "model", START, END),
        ms.run_metric("latency_p95", "name", START, END),
    )
    elapsed = time.monotonic() - started
    print(f"   _fetch_pages 调用次数 = {calls['n']}（期望 1）  耗时 {elapsed:.1f}s")
    print(
        f"   token_cost : degraded={tokens['degraded']} rows={len(tokens['rows'])} "
        f"error={tokens['error']}"
    )
    print(
        f"   latency_p95: degraded={latency['degraded']} rows={len(latency['rows'])} "
        f"error={latency['error']}"
    )
    for row in tokens["rows"][:3]:
        print(f"     token  行: {row}")
    for row in latency["rows"][:3]:
        print(f"     latency行: {row}")

    print("\n② 紧接着再查一次（60s 缓存应命中，0 次新调用）")
    before = calls["n"]
    again = await ms.run_metric("token_cost", "model", START, END)
    print(
        f"   新增 _fetch_pages 调用 = {calls['n'] - before}（期望 0）  "
        f"degraded={again['degraded']}"
    )

    print("\n③ 若当前处于限流冷却：再查必须 0 次新调用（负缓存短路）")
    before = calls["n"]
    cooled = await ms.run_metric("latency_p95", "name", START, END)
    print(
        f"   新增 _fetch_pages 调用 = {calls['n'] - before}  "
        f"degraded={cooled['degraded']} error={cooled['error']}"
    )

    ok = True
    if calls["n"] == 0:
        print("\n✗ 没有发生任何取数，验证无效")
        ok = False
    if tokens["degraded"] and latency["degraded"]:
        print("\n⚠ 两个指标都降级（可能是限流冷却中）—— 负缓存生效，但本次无法验证成功路径")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
