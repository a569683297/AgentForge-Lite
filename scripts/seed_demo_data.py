"""
示例数据播种脚本（seed_demo_data.py）—— 演示第 2 段「知识库」的前置
====================================================================
PRD §8.3 规划文件；把 PRD §17.2「示例演示文档清单」的三份文件上传进知识库。

## 为什么需要它

F10 的验收是「**全新环境 `docker compose up` 后 10 分钟可演示**」，而演示第 2 段就是
上传示例文档、跑知识库问答。但**新环境的知识库是空的**：

- `data/sample_docs/` 里若没有素材，别人 clone 下来**没有任何可上传的东西**；
- 靠"演示人自己准备文档"不成立 —— F10 的验收场景恰恰是**别人照着 README 自己跑**。

所以素材必须随仓库交付（三份文件已提交），这个脚本负责**把它们灌进库里**。

## 用法

    uv run python -m scripts.seed_demo_data            # 播种（已就绪的同名文档自动跳过）
    uv run python -m scripts.seed_demo_data --force    # 删除同名旧文档后重新上传
    uv run python -m scripts.seed_demo_data --clean    # 只删除这三份（演示结束后让库回到纯语料状态）
    uv run python -m scripts.seed_demo_data --api http://localhost:8000

## ⚠ 端口口径

默认连 **18000** —— 这是 `docker compose` 把 api 容器映射到宿主机的端口。
PRD §17.3 里写的是 `localhost:8000`，那是「宿主机直接 `uvicorn` 起 dev 服务」的口径。
两个后端实例都能用，但**工具装载数不同**（见 README「两个后端实例」一节）。

## 幂等性

按**文件名**判断：库里已有同名且 `status=ready` 的文档 → 跳过并打印 SKIP。
所以脚本可以反复跑，不会重复入库。

## 退出码（与 `warmup_mcp.py` 同构）

| 码 | 含义 |
|---|---|
| **0** | 三份文档全部 ready |
| **1** | 有文档落到 failed，或等待超时（**服务是活的，但入库没成功**） |
| **2** | 前置不满足：素材文件缺失 / 服务不可达（**不是坏了，是还没起**） |
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

SAMPLE_DIR = Path("data/sample_docs")

# PRD §17.2「示例演示文档清单」——顺序即演示时的上传顺序
SEEDS: tuple[tuple[str, str], ...] = (
    ("公司考勤制度.md", "考勤执行细则（补卡 / 加班费 / 各类假期天数）"),
    ("FAQ-常见问题.md", "员工高频问题 + 制度速查表"),
    ("产品手册.pdf", "产品规格（PDF，用于演示分页引用 p.N）"),
)

DEFAULT_API = "http://localhost:18000"
TERMINAL = {"ready", "failed"}
POLL_INTERVAL_S = 1.5


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def load_assets() -> list[tuple[str, str, bytes]]:
    """从 data/sample_docs/ 读出三份素材；缺失的会被跳过并记录。"""
    assets = []
    for filename, desc in SEEDS:
        path = SAMPLE_DIR / filename
        if not path.exists():
            print(f"  [❌] 素材缺失：{path}")
            continue
        data = path.read_bytes()
        print(f"  [✅] {filename:<20} {len(data):>8,} 字节   {desc}")
        assets.append((filename, desc, data))
    return assets


async def fetch_index(client: httpx.AsyncClient) -> dict[str, dict]:
    """GET /api/documents → {filename: 文档对象}（同名取最新一条）。"""
    resp = await client.get("/api/documents", params={"limit": 200})
    resp.raise_for_status()
    index: dict[str, dict] = {}
    for doc in resp.json():                      # 接口按上传时间倒序 → 后写的覆盖先写的
        index[doc["filename"]] = doc
    return index


async def upload(client: httpx.AsyncClient, filename: str, data: bytes) -> None:
    resp = await client.post(
        "/api/documents",
        files={"file": (filename, data, "application/octet-stream")},
    )
    resp.raise_for_status()
    body = resp.json()
    # 202 Accepted：只是"已受理"，此刻 status 必然是 processing（切片与向量化在后台跑）
    print(f"  [↑] {filename:<20} HTTP={resp.status_code} id={body['id'][:8]} status={body['status']}")


async def wait_settled(
    client: httpx.AsyncClient, names: set[str], timeout_s: float
) -> dict[str, dict]:
    """轮询直到关注的文档全部进入终态（ready / failed）或超时。"""
    deadline = time.monotonic() + timeout_s
    last: dict[str, dict] = {}
    while True:
        index = await fetch_index(client)
        last = {n: index[n] for n in names if n in index}
        pending = [n for n, d in last.items() if d["status"] not in TERMINAL]
        if not pending or time.monotonic() >= deadline:
            return last
        print(f"      … 等待入库：{', '.join(pending)}")
        await asyncio.sleep(POLL_INTERVAL_S)


async def run(args: argparse.Namespace) -> int:
    section("① 检查素材（PRD §17.2 清单）")
    assets = load_assets()
    if len(assets) != len(SEEDS):
        print("\n前置不满足：示例素材不完整，无法播种。")
        print(f"（应存在：{[f for f, _ in SEEDS]}，目录：{SAMPLE_DIR}）")
        return 2

    base_url = args.api.rstrip("/")
    print(f"\n目标服务：{base_url}")

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            # --- 连通性：这一步单独判，好把"服务没起"和"接口不对"分开 ---
            try:
                health = await client.get("/api/health")
                health.raise_for_status()
            except Exception as e:                          # noqa: BLE001 —— 这里要的就是兜住所有连接类异常
                print(f"  [❌] 服务不可达：{type(e).__name__}: {e}")
                print("       请先 `make up`（或 `docker compose up -d`）把服务起起来。")
                return 2
            print(f"  [✅] /api/health → {health.json()}")

            section("② 清点现有文档（幂等依据）")
            index = await fetch_index(client)
            print(f"  库内共 {len(index)} 份文档")

            if args.clean:
                return await do_clean(client, index)

            skip = {f for f, _, _ in assets if index.get(f, {}).get("status") == "ready"}
            if args.force:
                skip = set()
            to_upload = [(f, d) for f, _, d in assets if f not in skip]

            for f in sorted(skip):
                print(f"  [SKIP] {f:<20} 已入库且 ready（--force 可强制重传）")

            if not to_upload:
                print("\n三份示例文档均已就绪，无需上传。")
                return 0

            section("③ 上传（POST /api/documents → 202 Accepted）")
            for filename, data in to_upload:
                if args.force and filename in index:
                    old = index[filename]
                    print(f"  [×] 先删除旧文档 {filename}（{old['status']}）")
                    await client.delete(f"/api/documents/{old['id']}")
                await upload(client, filename, data)

            section(f"④ 等待入库完成（超时 {args.timeout:.0f}s）")
            names = {f for f, _ in to_upload}
            settled = await wait_settled(client, names, args.timeout)

            section("⑤ 结果")
            failed: list[str] = []
            for filename, desc in SEEDS:
                doc = settled.get(filename)
                if doc is None:
                    failed.append(filename)
                    print(f"  [❌] {filename:<20} 未出现在文档列表")
                    continue
                ok = doc["status"] == "ready"
                if not ok:
                    failed.append(filename)
                print(
                    f"  [{'✅' if ok else '❌'}] {filename:<20} {doc['status']:<10} "
                    f"切片={doc['chunk_count']:<3} {desc}"
                )

            final = await fetch_index(client)
            print(f"\n  库内现共 {len(final)} 份文档")

            if failed:
                print(f"\n有 {len(failed)} 份未就绪：{', '.join(failed)}")
                return 1

            print("\n三份示例文档全部 ready —— 演示第 2 段（知识库）可以开始。")
            return 0

    except httpx.HTTPError as e:
        print(f"\n[❌] 请求失败：{type(e).__name__}: {e}")
        return 1


async def do_clean(client: httpx.AsyncClient, index: dict[str, dict]) -> int:
    """删除三份示例文档（切片由数据库外键级联删除）。"""
    section("② 清理示例文档（--clean）")
    removed = 0
    for filename, _ in SEEDS:
        doc = index.get(filename)
        if doc is None:
            print(f"  [—] {filename:<20} 不在库内")
            continue
        resp = await client.delete(f"/api/documents/{doc['id']}")
        ok = resp.status_code == 204
        removed += 1 if ok else 0
        print(f"  [{'✅' if ok else '❌'}] {filename:<20} HTTP={resp.status_code}")
    print(f"\n已删除 {removed} 份（库内原有 {len(index)} 份）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 PRD §17.2 的三份示例演示文档播种进知识库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api", default=DEFAULT_API, help=f"后端地址（默认 {DEFAULT_API}）")
    parser.add_argument("--force", action="store_true", help="删除同名旧文档后重新上传")
    parser.add_argument("--clean", action="store_true", help="只删除这三份示例文档，不上传")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待入库的秒数上限（默认 120）")
    args = parser.parse_args()

    print("示例数据播种 —— PRD §17.2 示例演示文档清单")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
