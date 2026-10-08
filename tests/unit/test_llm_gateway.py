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


def test_locked_channel_returns_exactly_one(llm_channels_ready):
    """显式指定通道 = 锁定：只返回这一个，标签里要写清是锁定。"""
    channels = _resolve_channels("deepseek", allow_failover=True)
    assert len(channels) == 1
    config, label = channels[0]
    assert config["model"] == "fake-main-model"
    assert "锁定" in label


def test_locked_channel_ignores_allow_failover(llm_channels_ready):
    """
    ★ 指定了通道时，`allow_failover` 必须被**忽略**。
    否则"锁定"这个词就没有意义了 —— 它变成了"优先用它"。
    这正是 D22 judge 必须锁通道的由来（否则报告上写一个模型名，实际两个都用了）。
    """
    assert len(_resolve_channels("deepseek", allow_failover=True)) == 1
    assert len(_resolve_channels("deepseek", allow_failover=False)) == 1


def test_unknown_channel_name_raises_instead_of_falling_back(llm_channels_ready):
    """
    名字写错时必须**当场抛错**。
    静默回退到主通道会让 `.env` 里写错的配置"看起来工作正常"，
    而分数悄悄来自另一个模型 —— 这种故障没有任何日志线索。
    """
    with pytest.raises(ValueError, match="未知 LLM 通道"):
        _resolve_channels("gpt-5-turbo", allow_failover=True)


def test_locked_channel_without_key_raises_a_useful_message(llm_channels_ready, monkeypatch):
    """
    ★ 锁定的通道没配 key 时，报错必须**点出是哪个通道**。

    如果沿用降级循环里那句 `continue`，会走到"所有通道均失败"分支而 `last_error`
    仍是 None，最终报出"所有 LLM 通道均失败: None" —— 真正的病因（少配 key）被完全埋掉。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(RuntimeError, match="openai"):
        _resolve_channels("openai", allow_failover=True)


def test_failover_true_includes_backup_channel(llm_channels_ready):
    channels = _resolve_channels(None, allow_failover=True)
    assert [c["model"] for c, _ in channels] == ["fake-main-model", "fake-backup-model"]


def test_failover_false_keeps_only_primary(llm_channels_ready):
    channels = _resolve_channels(None, allow_failover=False)
    assert [c["model"] for c, _ in channels] == ["fake-main-model"]


def test_backup_absent_when_no_openai_key(llm_channels_ready, monkeypatch):
    """没配备用通道的 key 时，通道列表就只有一个 —— 不是"尽力而为"多塞一个空的。"""
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    channels = _resolve_channels(None, allow_failover=True)
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
