"""
单元：LLM 网关的**通道选择**与**降级链**（D7 双通道，D22 锁定通道）
====================================================================
网关是架构红线（业务代码不得直连 SDK），它自己也是最容易悄悄出错的地方：
"降级"这个动作会把"到底用了哪个模型"从数据里抹掉 —— 所以选择逻辑必须可测。

测试手法：替换 `_call_once`（单通道调用点），**保留** `_call_with_failover`
（降级循环）的真实实现 —— 否则测的是 mock 自己。
"""

import pytest

from app.services import llm_gateway as gateway
from app.services.llm_gateway import _resolve_channels, chat, chat_with_tools

# ============================================================
# 一、通道选择（纯逻辑，不发起任何调用）
# ============================================================


# ⚠ S2：`_resolve_channels` 从同步改成了 **async**（锁定分支要查 llm_models 表）。
#    这些用例随之改为 async + await —— pytest 的 `asyncio_mode = "auto"` 直接支持。
async def test_locked_channel_returns_exactly_one(llm_channels_ready):
    """显式指定通道 = 锁定：只返回这一个，标签里要写清是锁定。"""
    channels = await _resolve_channels("deepseek", allow_failover=True)
    assert len(channels) == 1
    config, label = channels[0]
    assert config["model"] == "fake-main-model"
    assert "锁定" in label


async def test_locked_channel_ignores_allow_failover(llm_channels_ready):
    """
    ★ 指定了通道时，`allow_failover` 必须被**忽略**。
    否则"锁定"这个词就没有意义了 —— 它变成了"优先用它"。
    这正是 D22 judge 必须锁通道的由来（否则报告上写一个模型名，实际两个都用了）。
    """
    assert len(await _resolve_channels("deepseek", allow_failover=True)) == 1
    assert len(await _resolve_channels("deepseek", allow_failover=False)) == 1


async def test_unknown_channel_name_raises_instead_of_falling_back(llm_channels_ready):
    """
    名字写错时必须**当场抛错**。
    静默回退到主通道会让 `.env` 里写错的配置"看起来工作正常"，
    而分数悄悄来自另一个模型 —— 这种故障没有任何日志线索。
    """
    with pytest.raises(ValueError, match="未知 LLM 通道"):
        await _resolve_channels("gpt-5-turbo", allow_failover=True)


async def test_locked_channel_without_key_raises_a_useful_message(llm_channels_ready, monkeypatch):
    """
    ★ 锁定的通道没配 key 时，报错必须**点出是哪个通道**。

    如果沿用降级循环里那句 `continue`，会走到"所有通道均失败"分支而 `last_error`
    仍是 None，最终报出"所有 LLM 通道均失败: None" —— 真正的病因（少配 key）被完全埋掉。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(RuntimeError, match="openai"):
        await _resolve_channels("openai", allow_failover=True)


async def test_failover_true_includes_backup_channel(llm_channels_ready):
    channels = await _resolve_channels(None, allow_failover=True)
    assert [c["model"] for c, _ in channels] == ["fake-main-model", "fake-backup-model"]


async def test_failover_false_keeps_only_primary(llm_channels_ready):
    channels = await _resolve_channels(None, allow_failover=False)
    assert [c["model"] for c, _ in channels] == ["fake-main-model"]


async def test_backup_absent_when_no_openai_key(llm_channels_ready, monkeypatch):
    """没配备用通道的 key 时，通道列表就只有一个 —— 不是"尽力而为"多塞一个空的。"""
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    channels = await _resolve_channels(None, allow_failover=True)
    assert len(channels) == 1


# ============================================================
# 二、降级链（真跑 `_call_with_failover`，只把最内层换成 mock）
# ============================================================


async def test_chat_returns_content_from_primary(llm_channels_ready, mock_llm):
    mock_llm.reply = "主通道的回答"
    assert await chat([{"role": "user", "content": "你好"}]) == "主通道的回答"
    assert mock_llm.models_called == ["fake-main-model"]


async def test_primary_failure_falls_back_to_backup(llm_channels_ready, mock_llm):
    """
    ★ 降级的定义：主通道失败 → **自动**切备用，调用方毫无感知。
    断言"两个模型都被调过、且顺序是主→备"才能证明降级真的发生了。
    """
    mock_llm.fail_models = {"fake-main-model"}
    mock_llm.reply = "备用的回答"

    assert await chat([{"role": "user", "content": "你好"}]) == "备用的回答"
    assert mock_llm.models_called == ["fake-main-model", "fake-backup-model"]


async def test_all_channels_down_raises_wrapped_error(llm_channels_ready, mock_llm):
    """两条路都挂 → 抛错（这是"降级"能兜住的最后边界）。"""
    mock_llm.fail_models = {"fake-main-model", "fake-backup-model"}
    with pytest.raises(RuntimeError, match="所有 LLM 通道均失败"):
        await chat([{"role": "user", "content": "你好"}])


async def test_locked_channel_failure_is_not_wrapped(llm_channels_ready, mock_llm):
    """
    ★ 锁定模式**不包装**异常：原文抛什么就抛什么。

    理由：调用方（judge 的重试逻辑）要靠异常类型判断"这个错值不值得重试" ——
    429 该重试、401 不该；一旦包成 RuntimeError，两者长得一模一样，
    信息就丢了。

    ⚠ 为什么让 mock 抛 `httpx.ConnectError` 而不是默认的 RuntimeError：
      网关包装后的异常**也是 RuntimeError**，拿 RuntimeError 去断言"没被包装"
      是恒真的（两边同一个类型）。换一个**不可能被包装出来**的类型，
      这条断言才有区分度。
    """
    import httpx

    mock_llm.error_type = httpx.ConnectError
    mock_llm.fail_models = {"fake-main-model"}

    with pytest.raises(httpx.ConnectError) as exc_info:
        await chat([{"role": "user", "content": "你好"}], provider="deepseek")

    assert not isinstance(exc_info.value, RuntimeError), "锁定模式的异常被包装了"
    assert mock_llm.models_called == ["fake-main-model"]  # 锁定 ⇒ 绝不偷偷换


async def test_chat_with_tools_returns_message_and_usage(llm_channels_ready, mock_llm):
    tools = [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}}]
    out = await chat_with_tools([{"role": "user", "content": "查一下"}], tools)

    assert "message" in out and "usage" in out
    assert mock_llm.calls[0]["tools"] == tools  # tools 确实透传下去了


async def test_temperature_is_passed_through(llm_channels_ready, mock_llm):
    """
    温度必须真的传到最内层（D14 的教训：摘要若被迫用 0.4 这个"创作档"，
    会加戏、改事实）—— 所以它不能在中途被默认值吃掉。
    """
    await chat([{"role": "user", "content": "压缩一下"}], temperature=0.1)
    assert mock_llm.calls[0]["temperature"] == 0.1


async def test_default_temperature_is_answer_scenario(llm_channels_ready, mock_llm):
    await chat([{"role": "user", "content": "你好"}])
    assert mock_llm.calls[0]["temperature"] == gateway.DEFAULT_TEMPERATURE


# ============================================================
# 三、S2 模型锁定（model_id → 单通道；解析失败必抛）与归因（model_used）
# ============================================================
import uuid as _uuid  # noqa: E402  （本段专用的 uuid 构造）


def _fake_custom_row(**overrides):
    """
    伪造一行 llm_models（只带解析器需要的属性）。

    为什么不用真 ORM 对象：这里的被测者是 `_resolve_locked_model` 的**取值逻辑**，
    不是「ORM 怎么映射」—— 后者由 S1 的模型测试与迁移自查覆盖。
    用 SimpleNamespace 把"查出来的行长什么样"钉成显式契约。
    """
    from types import SimpleNamespace

    base = {
        "id": _uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "name": "本地Qwen",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": None,            # 本地端点：不需要认证
        "model": "qwen2.5:7b",
        "enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_get_model(monkeypatch, row):
    """把 `model_service.get_model` 换成返回指定行的桩（None = 查无此行）。"""
    from app.services import model_service

    async def _fake_get(model_uuid):
        return row

    monkeypatch.setattr(model_service, "get_model", _fake_get)


async def test_model_id_builtin_locks_single_channel(llm_channels_ready):
    """
    S2：`model_id` 传内置通道名 → 锁定该通道（单元素列表），
    且通道上带有 `_model_id` / `_model_name` 注解（归因数据的来源）。
    """
    channels = await _resolve_channels(None, allow_failover=True, model_id="deepseek")

    assert len(channels) == 1, "model_id 锁定语义 = 只返回选中的那一个"
    config, label = channels[0]
    assert config["model"] == "fake-main-model"
    assert config["_model_id"] == "deepseek"
    assert "选定模型" in label
    assert gateway._model_used_of(config) == {
        "id": "deepseek",
        "name": "DeepSeek",
        "model": "fake-main-model",
    }


async def test_model_id_lock_ignores_failover_flag(llm_channels_ready):
    """与 provider 锁定同一条纪律：显式选中时，`allow_failover` 传什么都只给一个通道。"""
    for flag in (True, False):
        channels = await _resolve_channels(None, allow_failover=flag, model_id="deepseek")
        assert len(channels) == 1


async def test_model_id_builtin_without_key_is_resolution_error(llm_channels_ready, monkeypatch):
    """
    ★ 内置未配 key → `ModelResolutionError`（**不是** RuntimeError）。

    为什么钉异常类型：同样是"锁定通道没 key"，
      · provider 路径（judge 内部调用）→ RuntimeError；
      · model_id 路径（**用户输入**）→ ModelResolutionError → 路由层翻成 422。
    混用会让"用户点了没配 key 的模型"变成 HTTP 500 —— 用户输入问题和服务端故障
    的边界必须靠异常类型分清（路由层就是按类型分叉的）。
    """
    from app.config import settings
    from app.services.llm_gateway import ModelResolutionError

    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(ModelResolutionError, match="openai"):
        await _resolve_channels(None, allow_failover=True, model_id="openai")


async def test_model_id_neither_builtin_nor_uuid_is_resolution_error(llm_channels_ready):
    """既不是内置名、也不是合法 UUID → ModelResolutionError（报错信息要两种可能都点到）。"""
    from app.services.llm_gateway import ModelResolutionError

    with pytest.raises(ModelResolutionError, match="未知的模型 id"):
        await _resolve_channels(None, allow_failover=True, model_id="gpt-5-turbo")


async def test_model_id_custom_uuid_resolves_channel_without_key(llm_channels_ready, monkeypatch):
    """
    自定义 UUID 有效 → 解析出通道；**允许没有 api_key**（Ollama 场景）——
    必须带 `_allow_empty_key` 标记，否则降级循环会把它当"没配 key"静默跳过，
    表现成"选定模型后所有通道均失败"，而真正原因是端点压根没被调用。
    """
    row = _fake_custom_row()
    _patch_get_model(monkeypatch, row)

    channels = await _resolve_channels(None, allow_failover=True, model_id=str(row.id))

    assert len(channels) == 1
    config, label = channels[0]
    assert config["model"] == "qwen2.5:7b"
    assert config["_allow_empty_key"] is True
    assert config["_model_name"] == "本地Qwen"
    assert "本地Qwen" in label


async def test_model_id_custom_not_found_is_resolution_error(llm_channels_ready, monkeypatch):
    """被删除的模型 UUID → ModelResolutionError（**绝不静默回退主通道**）。"""
    from app.services.llm_gateway import ModelResolutionError

    _patch_get_model(monkeypatch, None)
    with pytest.raises(ModelResolutionError, match="不存在"):
        await _resolve_channels(
            None, allow_failover=True, model_id="11111111-2222-3333-4444-555555555555"
        )


async def test_model_id_custom_disabled_is_resolution_error(llm_channels_ready, monkeypatch):
    """被禁用的模型 → ModelResolutionError（enabled=False 是"置灰不删"的实现）。"""
    from app.services.llm_gateway import ModelResolutionError

    row = _fake_custom_row(enabled=False)
    _patch_get_model(monkeypatch, row)

    with pytest.raises(ModelResolutionError, match="禁用"):
        await _resolve_channels(None, allow_failover=True, model_id=str(row.id))


async def test_model_id_lock_failure_does_not_fall_back(llm_channels_ready, mock_llm):
    """
    ★ 全链锁定语义（真跑降级循环）：选定模型失败 → **原样抛出，不切备用通道**。

    与 test_locked_channel_failure_is_not_wrapped 同一手法的 model_id 版：
    异常类型换成 `httpx.ConnectError`（网关包装产物是 RuntimeError，拿它断言"没包装"
    恒真、没有区分度）；`models_called` 必须只有主通道的那一次 ——
    "降级是默认行为"的常识在这里被显式否证，这正是 S2 §4.3"锁定不降级"的机制证据。
    """
    import httpx
    from app.services.llm_gateway import chat_with_tools

    mock_llm.error_type = httpx.ConnectError
    mock_llm.fail_models = {"fake-main-model"}

    with pytest.raises(httpx.ConnectError):
        await chat_with_tools(
            [{"role": "user", "content": "你好"}], [], model_id="deepseek"
        )

    assert mock_llm.models_called == ["fake-main-model"], "锁定模型的失败不得静默降级"


async def test_chat_with_tools_reports_model_used(llm_channels_ready, mock_llm):
    """默认链路（不传 model_id）也要有归因 —— 且指向真实执行的通道。"""
    out = await chat_with_tools([{"role": "user", "content": "你好"}], [])

    assert out["model_used"] == {
        "id": "deepseek",
        "name": "DeepSeek",
        "model": "fake-main-model",
    }


async def test_model_used_follows_actual_executor_after_failover(llm_channels_ready, mock_llm):
    """
    ★ 归因的试金石：默认链路中主通道失败 → 自动降级到备用应答 ——
    `model_used` 必须报**备用**（真实执行者），而不是"配置上本应是谁"。
    归因写的是事实；写下配置名就会与"降级让用了哪个通道从数据里消失"的旧病同构。
    """
    mock_llm.fail_models = {"fake-main-model"}
    out = await chat_with_tools([{"role": "user", "content": "你好"}], [])

    assert out["model_used"]["id"] == "openai"
    assert out["model_used"]["model"] == "fake-backup-model"
