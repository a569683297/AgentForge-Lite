"""
集成（external）：真连 Harness SaaS —— **默认跳过**
=====================================================
用 `uv run pytest -m external` 才运行。

为什么它必须默认跳过：它依赖三样**本应用控制不了**的东西 ——
npx（Node 运行时）、真实 PAT、外网。把它们放进默认回归，会让
"账号过期 / 网络抖动"变成一片红 —— 而那是**外部状态**，不是代码回归。
（这正是 2026-10-06 那次"TRIAL 账号到期"暴露出来的教训：风险不会因为
 没人跑它而消失，但也不该让它污染回归信号 —— 分开跑是唯一同时满足两者的做法。）

建议先 `make warmup`：预热会把"首次拉 npm 包"的成本提前付掉，
否则这里的 connect 可能要等几分钟。
"""

import pytest

pytestmark = pytest.mark.external


async def test_harness_spec_can_be_built(monkeypatch):
    """
    第一层：**不联网**，只验证"配置能不能拼出一份合法的 harness 启动说明"。

    刻意复用 `build_specs()`（生产同一条路径），而不是在测试里自己拼 spec ——
    自己拼等于测自己，环境变量白名单那些坑一个都测不出来。
    """
    from app.config import settings
    from app.mcp import build_specs

    monkeypatch.setattr(settings, "mcp_harness_enabled", True)
    specs = {s.name: s for s in build_specs(settings)}

    assert "harness" in specs, "build_specs 没有产出 harness"
    spec = specs["harness"]
    assert spec.command == settings.mcp_harness_command
    assert spec.args  # 不能为空
    assert spec.namespace == "", "harness 工具名自带前缀，namespace 必须为空"


async def test_harness_connects_and_lists_tools(monkeypatch):
    """第二层：真起进程 + 握手 + `tools/list`（**只读**）。"""
    from app.config import settings
    from app.mcp import build_specs
    from app.mcp.client import MCPClient

    if not settings.harness_api_key:
        pytest.skip("未配置 HARNESS_API_KEY")
    monkeypatch.setattr(settings, "mcp_harness_enabled", True)

    spec = {s.name: s for s in build_specs(settings)}["harness"]
    client = MCPClient(spec)
    await client.connect(timeout_s=180)
    try:
        names = await client.discover_and_register()
        assert names, "一个工具都没列出来"
        # 契约：harness 的工具名**自带** harness_ 前缀（所以 namespace 传空串）
        assert all(n.startswith("harness_") for n in names)
        assert client.protocol_version, "没拿到协议版本"
    finally:
        await client.close()


async def test_harness_readonly_call_returns_real_data(monkeypatch):
    """
    第三层：真调一次**只读**工具（补 2026-10-06 到期日之后挂起的那笔账）。

    ⚠ 边界（别把结论说大）：只验只读路径。
      写操作（create/update/delete/execute）不在本用例里 ——
      所以通过它只能说"只读路径可用"，**不能说"账号没问题"**。
    """
    from app.config import settings
    from app.mcp import build_specs
    from app.mcp.client import MCPClient

    if not settings.harness_api_key:
        pytest.skip("未配置 HARNESS_API_KEY")
    monkeypatch.setattr(settings, "mcp_harness_enabled", True)

    spec = {s.name: s for s in build_specs(settings)}["harness"]
    client = MCPClient(spec)
    await client.connect(timeout_s=180)
    try:
        names = await client.discover_and_register()
        # 零硬编码：挑一个"名字里带 list 的只读工具"，而不是写死 harness_list
        readonly = sorted(n for n in names if n.endswith("_list") or n.endswith("list"))
        assert readonly, f"没有可用的只读工具：{names}"

        out = await client.call(readonly[0], {"resource_type": "organization"})
        assert isinstance(out, str)
        assert "执行失败" not in out and "401" not in out, f"只读调用失败：{out[:300]}"
    finally:
        await client.close()
