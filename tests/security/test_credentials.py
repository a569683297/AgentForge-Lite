"""
安全：凭据不外泄 + 写操作确认门（PRD §14 安全层）
===================================================
PRD 风险表里两条直接相关：
  · 「API key 泄漏」→ 缓解措施写的是".env 不入 git；用户重置 DeepSeek key"
  · S13「工具写操作防护：100% 写操作需显式确认」

前者只说到了**文件层面**（不进 git），但还有一条更容易被忽略的路径：
**配置对象被打印进日志**。这条路径没有任何告警，出事时也不会有症状。
"""

import pytest


def _secret_fields() -> dict[str, str]:
    """取出所有"绝不能出现在日志或响应里"的配置值（只保留非空的）。"""
    from app.config import settings

    return {
        "deepseek_api_key": settings.deepseek_api_key,
        "openai_api_key": settings.openai_api_key,
        "langfuse_secret_key": settings.langfuse_secret_key,
        "harness_api_key": settings.harness_api_key,
    }


def _configured_secrets() -> dict[str, str]:
    return {k: v for k, v in _secret_fields().items() if v}


# ============================================================
# 一、配置对象被打印时会泄露什么
# ============================================================


def test_settings_repr_does_not_leak_secrets():
    """
    ★ pydantic 的 `repr()` 默认会打印**所有字段的实际值**。

    这意味着任何一处 `logger.info("config=%s", settings)`、任何一次
    把 `settings` 塞进异常消息、乃至调试时在 REPL 里敲一下变量名，
    都会把 API key 明文写进日志/终端 —— 而它**完全不像"泄密"**，
    看起来只是打了一行配置。

    这条断言把"配置文件本身"当成泄露面来测（不只是"文件进没进 git"）。
    """
    from app.config import settings

    text = repr(settings)
    leaked = [name for name, value in _configured_secrets().items() if value in text]
    assert leaked == [], f"repr(settings) 里出现了明文密钥：{leaked}"


def test_settings_str_does_not_leak_secrets():
    """`str()` 走的是同一个 repr 通道，一并钉住。"""
    from app.config import settings

    text = f"{settings}"
    leaked = [name for name, value in _configured_secrets().items() if value in text]
    assert leaked == [], f"str(settings) 里出现了明文密钥：{leaked}"


@pytest.mark.db
async def test_tools_endpoint_response_contains_no_secret(api_client):
    """
    `/api/tools` 是唯一会把"配置相关元信息"吐给前端的接口
    （它带 servers 状态块）。响应体里绝不能出现任何密钥值。
    """
    body = (await api_client.get("/api/tools")).text
    leaked = [name for name, value in _configured_secrets().items() if value in body]
    assert leaked == [], f"/api/tools 响应里出现了密钥：{leaked}"


@pytest.mark.db
async def test_health_endpoint_contains_no_secret(api_client):
    """健康检查也会被外部探针轮询，响应是公开可读的。"""
    body = (await api_client.get("/api/health")).text
    leaked = [name for name, value in _configured_secrets().items() if value in body]
    assert leaked == [], f"/api/health 响应里出现了密钥：{leaked}"


# ============================================================
# 二、写操作确认门（S13 / F11.4）
# ============================================================


def test_harness_read_only_is_on_by_default():
    """
    ★ 只读开关**默认必须是开**的。

    理由不是"保守一点更好"，而是**个人 PAT 做不到只读** ——
    它继承账号全部权限，创建时没有"只读档"。所以"只读"只能靠服务端这个
    配置变量屏蔽 create/update/delete/execute。
    默认关掉 = 写操作在用户毫不知情的情况下被放行。

    前后两条一起断言：
      · 默认值 —— 这是代码契约（与 `.env` 无关）
      · 当前生效值 —— `.env` 若把它改成 false，那是"有人显式放宽了写操作"，
        不该在无人察觉的情况下发生
    """
    from app.config import Settings, settings

    assert Settings.model_fields["mcp_harness_read_only"].default is True
    assert settings.mcp_harness_read_only is True


def test_risk_level_must_come_from_the_whitelist():
    """
    写操作的人工确认阈值只能取白名单里的值。

    `HARNESS_RISK_LEVELS` 是**唯一合法取值清单**（配置校验与文档都从这里取）——
    所以断言"当前值在里面"等于同时钉住了"清单没被改窄"和"配置没写错"。
    """
    from app.config import HARNESS_RISK_LEVELS, settings

    assert settings.mcp_harness_auto_approve_risk in HARNESS_RISK_LEVELS
    # 最保守的那一档必须存在（它是默认值，也是"全都要确认"的语义）
    assert "none" in HARNESS_RISK_LEVELS


def test_harness_is_disabled_by_default():
    """
    外部 Harness server **默认关**。它每次启动都要拉一个常驻 npm 子进程
    （冷启动可能几分钟），而绝大多数开发/回归根本用不到 ——
    默认开着会让"跑个测试"变成"等 npm"。

    ⚠ 断言的是**字段默认值**，不是当前生效值：
      当前值受 `.env` 影响（本项目里 `MCP_HARNESS_ENABLED=true` 是显式打开的），
      用它来断言"默认关"是**测错了对象** —— 测的是环境，不是代码。
    """
    from app.config import Settings

    assert Settings.model_fields["mcp_harness_enabled"].default is False


def test_harness_version_is_pinned_not_latest():
    """
    ★ Harness 的版本必须**锁死**，不能写 `@latest`。

    PRD 风险表点名了「Harness MCP Server 迭代过快」，缓解措施就是"锁版本号"。
    而 `@latest` 是个**会动的指针**：npx 每次启动都要联网问 registry
    "latest 现在是哪个"，于是"能不能起来"依赖网络抖动，版本还会静默地漂
    （已观测：D27 记到 3.2.31 → 10-07 复查已是 3.2.32，没人改过配置）。
    """
    from app.config import settings

    assert "@latest" not in settings.mcp_harness_args, "版本又漂回 @latest 了"
    assert "@" in settings.mcp_harness_args, "没写版本号"


def test_npm_registry_mirror_is_configured():
    """
    国内直连 `registry.npmjs.org` 实测超时（curl 12s 返回 000）。
    镜像没配的话，首次预热会挂住 5 分钟以上**且没有任何输出**。
    """
    from app.config import settings

    assert "npmmirror" in settings.mcp_npm_registry or "taobao" in settings.mcp_npm_registry
