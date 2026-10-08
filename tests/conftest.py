"""
测试公共夹具（D34）
====================
三条纪律（决定了"为什么这些用例在 CI 上不会因为网络而红"）：

1. **默认不依赖外网** —— LLM 一律走 mock；MCP 走本机自研的 `inventory_server.py`
   （真协议、真子进程，但不碰网络、不要 PAT）；只有 Harness（外部 SaaS）标 `external`。
2. **不碰 `.env`** —— 所有配置覆盖都用 `monkeypatch`，测试结束自动还原；
   绝不为了跑测试去改配置文件。
3. **默认不写库** —— 集成测试优先打只读接口；确需写的一律自己清理。

术语（第一次出现处就地注解）：
- **夹具（fixture）**：pytest 的"测试前置准备"。在函数参数里写上夹具名，
   pytest 就会先执行它、把返回值交进来，用例结束再执行清理。
- **monkeypatch**：pytest 内置夹具，用来临时替换对象的属性/函数，
   用例一结束**自动还原**（所以不需要自己写 try/finally）。
- **标记（mark）**：给用例贴的标签，可以用来分组、选择性跳过。
"""

from contextlib import contextmanager

import pytest
from httpx import ASGITransport, AsyncClient

# ============================================================
# 一、`external` 标记默认跳过（PRD §14 明确要求）
# ============================================================


def pytest_collection_modifyitems(config, items):
    """
    默认跳过所有 `@pytest.mark.external` 的用例；`-m external` 时才运行它们。

    为什么不用 `addopts = "-m 'not external'"`：
      那样命令行再写 `-m external` 会变成"既要 external 又要 not external"，
      自相矛盾 → 永远收集不到任何用例，而且**不报错**（静默 0 条）。
      这里改成"没显式点名 external 才跳过"，两种写法都能正常工作。
    """
    expr = config.getoption("-m", default="") or ""
    if "external" in expr:
        return
    skip_marker = pytest.mark.skip(
        reason="需要外部服务（Harness SaaS / npx / PAT）；用 `-m external` 显式运行"
    )
    for item in items:
        if "external" in item.keywords:
            item.add_marker(skip_marker)


# ============================================================
# 二、API 客户端
# ============================================================


@pytest.fixture
async def api_client():
    """
    直接打 FastAPI 应用的 HTTP 客户端：**不开真端口、不起 uvicorn**。

    ⚠ 用 `ASGITransport` 而不是 `TestClient(app)`：
      `TestClient` 是同步的（内部塞进线程跑），而本项目整套后端是 async ——
      用 `AsyncClient + ASGITransport` 才是和线上**同一条**调用路径。

    ⚠ 它**不会触发 lifespan**（这正是我们要的）：lifespan 会去起 MCP 子进程、
      连外部 server，测试不该把"启动外部依赖"变成跑用例的前提。
    """
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================
# 三、LLM mock（PRD §14 的 mock 策略）
# ============================================================


class FakeLangfuse:
    """
    顶掉真 Langfuse 客户端：不联网、不落 trace。

    网关每次调用都会 `with langfuse.start_as_current_observation(...)` ——
    换成真的就会朝 Langfuse Cloud 发请求，测试会被网络拖慢甚至红。
    """

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        class _Observation:
            def update(self, **kw):  # 网关会调它记 level/usage，这里吞掉
                return None

        yield _Observation()


class MockLLM:
    """
    确定性假模型。

    替换的是网关的**单通道调用点** `_call_once`，**不是** `chat()` 本身 ——
    差别很关键：`_call_once` 之上还有**降级循环**（主通道失败 → 切备用），
    把它留着，"降级"这段真实逻辑才测得到；否则测的是 mock 自己。

    用法：
        mock_llm.fail_models = {"deepseek-chat"}   # 让这个模型一律抛错
        mock_llm.calls                             # 查看被调过几次、用的哪个模型
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_models: set[str] = set()
        self.reply = "（mock）好的"
        self.tool_calls: list | None = None
        # 注入故障时抛什么异常类型。**可换**，因为有一类断言专门要验证
        # "异常没有被包装" —— 若 mock 抛的就是 RuntimeError，而网关的包装也产
        # RuntimeError，那条断言两边长得一样，等于恒真。
        self.error_type: type[Exception] = RuntimeError

    async def __call__(
        self,
        provider: dict,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        trace_name: str = "",
        gen=None,
        temperature: float = 0.4,
    ) -> dict:
        self.calls.append(
            {
                "model": provider.get("model"),
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "trace_name": trace_name,
            }
        )
        if provider.get("model") in self.fail_models:
            raise self.error_type(f"mock 故障注入：{provider.get('model')} 不可用")

        if tools and self.tool_calls and len(self.calls) == 1:
            # 第一轮：声明一次工具调用。**只在第一轮**这么做 ——
            # 之后各轮改为直答，否则 Agent 图会一直"调工具→再规划"循环到 recursion_limit。
            message = {"role": "assistant", "content": None, "tool_calls": self.tool_calls}
        else:
            # 直答：`chat()` 取 content，`plan_node` 也把它当最终回答（无 tool_calls → END）
            message = {"role": "assistant", "content": self.reply}
        return {
            "data": {"choices": [{"message": message}]},
            "usage": {"total_tokens": 7},
        }

    @property
    def models_called(self) -> list[str]:
        """按顺序返回被调用过的模型名（断言"降级到了哪个通道"用）。"""
        return [c["model"] for c in self.calls]


@pytest.fixture
def mock_llm(monkeypatch):
    """
    把网关的单通道调用点换成 MockLLM，并顺手静音 Langfuse。

    ⚠ 只 patch 这一个函数：`_call_with_failover` / `chat` / `chat_with_tools`
      全部保持真实实现，所以"降级链"是被真跑出来的，不是被 mock 掉的。
    """
    from app.services import llm_gateway as gateway

    fake = MockLLM()
    monkeypatch.setattr(gateway, "_call_once", fake)
    monkeypatch.setattr(gateway, "langfuse", FakeLangfuse())
    return fake


@pytest.fixture
def llm_channels_ready(monkeypatch):
    """
    给两个 LLM 通道都塞上假 key，让 `primary_llm` / `backup_llm` 都"可用"。

    为什么要它：没 key 的通道会被降级循环 `continue` 跳过，
    于是"主 → 备"的降级根本走不起来，测试会以"通道列表为空"的方式失败，
    看起来像代码坏了，其实是夹具没准备。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "fake-main-key")
    monkeypatch.setattr(settings, "deepseek_chat_model", "fake-main-model")
    monkeypatch.setattr(settings, "openai_api_key", "fake-backup-key")
    monkeypatch.setattr(settings, "openai_chat_model", "fake-backup-model")
    return settings


# ============================================================
# 四、工具注册表隔离
# ============================================================


@pytest.fixture
def registry():
    """
    直接操作全局工具注册表，**用完还原**。

    为什么必须还原：`_registry` 是模块级全局字典，测试往里注册的假工具
    会**留到下一个用例**里 —— 那种"上一个测试的残留让下一个测试变得不可信"
    的故障，排查成本远高于写这几行还原代码。
    """
    from app.tools import registry as reg

    snapshot = dict(reg._registry)
    yield reg
    reg._registry.clear()
    reg._registry.update(snapshot)
