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

# 测试账号的固定邮箱。用固定值而不是随机值，是为了让"上一次跑崩了留下的数据"
# 能在下一次直接复用，而不是每跑一次多一个孤儿账号。
TEST_ADMIN_EMAIL = "pytest-admin@agentforge.local"
TEST_USER_EMAIL = "pytest-user@agentforge.local"
TEST_PASSWORD = "PytestPass123"


async def _ensure_test_user(email: str, role: str):
    """
    保证库里有一个测试账号（不存在就建，角色不对就改），返回 ORM 行。

    为什么直接写库而不是调 `auth_service.register`：
      register 用**生产成本因子 12**（166ms）。测试里每建一次账号就慢一次，
      而且我们根本不校验密码复杂度这条路径。这里用 rounds=4（0.7ms）建，
      `verify_password` 会自动从哈希串里读出成本因子，校验照样正确。
    """
    from sqlalchemy import select

    from app.core.db import async_session_factory
    from app.core.security import hash_password
    from app.models.user import User

    async with async_session_factory() as session:
        row = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if row is None:
            row = User(
                email=email,
                password_hash=hash_password(TEST_PASSWORD, rounds=4),
                display_name="pytest",
                role=role,
                is_active=True,
            )
            session.add(row)
        else:
            row.role = role
            row.is_active = True
        await session.commit()
        await session.refresh(row)
        return row


def _access_token(user) -> str:
    """给测试用户签一个真 access token（走与线上同一个 create_token）。"""
    from app.core.security import TOKEN_TYPE_ACCESS, create_token

    token, _, _ = create_token(user.id, token_type=TOKEN_TYPE_ACCESS, role=user.role)
    return token


@pytest.fixture
async def admin_user():
    """**管理员**测试账号 —— 默认 `api_client` 用它。

    为什么默认是 admin：账号体系上线前，所有接口都是"谁都能调"。
    上线后 eval / traces / metrics / tool-servers 都收成了管理员能力，
    若默认账号是普通用户，那些用例会集体 403 —— 而它们测的是业务逻辑，
    不是权限（权限有专门的角色用例）。用 admin 打底，等于保持原有覆盖面。
    """
    return await _ensure_test_user(TEST_ADMIN_EMAIL, "admin")


@pytest.fixture
async def normal_user():
    """**普通用户**测试账号 —— 跨用户隔离用例用它（A 看不到 B 的数据）。"""
    return await _ensure_test_user(TEST_USER_EMAIL, "user")


@pytest.fixture
async def api_client(admin_user):
    """
    直接打 FastAPI 应用的 HTTP 客户端：**不开真端口、不起 uvicorn**。

    ⚠ 用 `ASGITransport` 而不是 `TestClient(app)`：
      `TestClient` 是同步的（内部塞进线程跑），而本项目整套后端是 async ——
      用 `AsyncClient + ASGITransport` 才是和线上**同一条**调用路径。

    ⚠ 它**不会触发 lifespan**（这正是我们要的）：lifespan 会去起 MCP 子进程、
      连外部 server，测试不该把"启动外部依赖"变成跑用例的前提。

    账号体系之后（2026-10-21）：**默认带 Bearer 头**（管理员）。
      这是刻意的 —— 60 多个既有集成用例测的是业务行为，让它们每条都自己拼
      Authorization 既啰嗦又会把"忘了带 token"变成一种新的假失败。
      要测"没登录会怎样"，用下面的 `anon_client`；要测越权，用 `user_client`。
    """
    from app.main import app

    transport = ASGITransport(app=app)
    token = _access_token(admin_user)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@pytest.fixture
async def anon_client():
    """**不带任何凭证**的客户端 —— 专测 401 / 注册登录这些入口。"""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def user_client(normal_user):
    """带**普通用户**凭证的客户端 —— 跨用户隔离（越权）用例用它。"""
    from app.main import app

    transport = ASGITransport(app=app)
    token = _access_token(normal_user)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
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

    # 固定的假 trace id（32 位十六进制，与 Langfuse 的真实格式同宽）。
    # 为什么给一个**真值**而不是 None：`agent_service` 会把它写进 messages.trace_id，
    # 返回 None 就等于让"写入 trace_id"这段代码在测试里永远走不到 ——
    # 而它正是 Trace 页「按提问展示」的支点。
    FAKE_TRACE_ID = "0" * 8 + "abcdef" + "0" * 8 + "fedcba" + "0" * 2

    def get_current_trace_id(self) -> str:
        """
        当前 trace id。**这个方法是补上的，不是一开始就有的**：
        加了 messages.trace_id 之后，`agent_service.current_trace_id()` 会调它，
        而 FakeLangfuse 没有 → `AttributeError: 'FakeLangfuse' object has no attribute
        'get_current_trace_id'`，一整片 agent 集成测试直接红。
        教训：给产品代码新增一个**外部依赖的方法调用**时，替身类要同步补齐；
        mock 不是"随便糊一个"就完事的东西。
        """
        return self.FAKE_TRACE_ID

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        class _Observation:
            def update(self, **kw):  # 网关会调它记 level/usage，这里吞掉
                return None

        yield _Observation()


@pytest.fixture
def silence_langfuse(monkeypatch):
    """
    静音 Langfuse：把**两处**模块级引用都换成 FakeLangfuse。

    ⚠ 为什么必须是两处（实测踩坑，10-16 修复）：
      · `llm_gateway.langfuse`    —— 网关 generation 埋点的**属性引用**，patch 它有效；
      · `observability.langfuse`  —— `from ... import langfuse` 的**导入时绑定**：
        patch 网关模块的属性**改不到它**。只 patch 网关时，observability 的 span
        （run_agent / tool / retrieval 整棵树）仍然打真 Langfuse —— 症状是
        每跑一轮集成测试，Langfuse 上就多一批"没有 generation 的 run_agent 树"
        （latency 只有 0.001s 的假数据），测试数据污染了可观测性数据，且全程不报错。
      → 修复后跑测试不再产生任何 Langfuse 数据（当天以"跑测试 → 拉最近数据无新增"复核）。
    """
    from app.services import llm_gateway as gateway
    from app.services import observability

    fake = FakeLangfuse()
    monkeypatch.setattr(gateway, "langfuse", fake)
    monkeypatch.setattr(observability, "langfuse", fake)
    return fake


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
def mock_llm(monkeypatch, silence_langfuse):
    """
    把网关的单通道调用点换成 MockLLM，并静音 Langfuse（后者由 `silence_langfuse`
    统一负责 —— 它同时覆盖 observability 的 span 树，见该 fixture 的说明）。

    ⚠ 只 patch `_call_once` 这一个函数：`_call_with_failover` / `chat` /
      `chat_with_tools` 全部保持真实实现，所以"降级链"是被真跑出来的，
      不是被 mock 掉的。
    """
    from app.services import llm_gateway as gateway

    fake = MockLLM()
    monkeypatch.setattr(gateway, "_call_once", fake)
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
