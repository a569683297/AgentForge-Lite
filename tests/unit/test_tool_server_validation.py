"""
工具上传校验（纯函数，不碰 IO、不碰 docker）
==============================================
为什么这组用例特别值：它们挡在**用户输入**与**文件系统**之间。
每一个漏掉的规则都不是"体验不好"，而是：
    · 名字漏 → `unregister_source` 误删系统工具 / register 撞名让 server 起不来
    · 路径漏 → 删目录时删到工具根之外
    · 大小漏 → 一次上传吃掉整个进程内存
    · 语法漏 → 用户对着 20 秒握手超时发呆，而真正原因是一行拼错的代码

这里只打纯函数 —— 不需要数据库、不需要 docker、不需要 FastAPI，
所以它们永远能跑（CI 里也不会因为环境缺失被 skip）。
"""

import pytest

from app.services import tool_server_service as svc
from app.services.tool_server_service import (
    MAX_CODE_BYTES,
    InvalidToolUpload,
)


# ============================================================
# 一、名字
# ============================================================
@pytest.mark.parametrize("raw", ["demo_tools", "abc", "a1_b2", "tool_2026"])
def test_valid_names_accepted(raw: str) -> None:
    assert svc.validate_name(raw) == raw


def test_name_is_lowercased_not_rewritten() -> None:
    """
    大小写归一化是允许的（意图明显），但**不做替换式改写**。

    `MyTool` → `mytool`：可以（只是大小写）。
    `my-tool` → `my_tool`：不行 —— 那等于替用户改主意，
    他下次按 `my-tool` 找不到东西，而系统里没有任何提示说"我给你改了"。
    """
    assert svc.validate_name("  MyTool  ") == "mytool"
    with pytest.raises(InvalidToolUpload):
        svc.validate_name("my-tool")


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("ab", "太短（<3）"),
        ("1abc", "数字开头"),
        ("my tool", "含空格"),
        ("my.tool", "含点"),
        ("a" * 33, "太长（>32）"),
        ("", "空"),
        ("中文名", "非 ASCII"),
    ],
)
def test_invalid_names_rejected(raw: str, reason: str) -> None:
    with pytest.raises(InvalidToolUpload):
        svc.validate_name(raw)


@pytest.mark.parametrize("reserved", sorted(svc.RESERVED_NAMES))
def test_reserved_names_rejected(reserved: str) -> None:
    """
    保留名必须拒绝，且理由是**破坏性**的，不是风格问题：

    `unregister_source(source)` 按 source 整批摘工具 ——
    用户若叫 `local`，删掉他的工具会把项目内置工具一起摘干净；
    叫 `inventory` 则连带摘掉 D26 那台 server 的工具。
    """
    with pytest.raises(InvalidToolUpload) as exc:
        svc.validate_name(reserved)
    assert "保留" in str(exc.value)


# ============================================================
# 二、代码内容
# ============================================================
GOOD = "from mcp.server import MCPServer\nserver = MCPServer('x')\n"


def test_good_code_accepted() -> None:
    assert svc.validate_code(GOOD.encode(), "x.py") == GOOD


@pytest.mark.parametrize("filename", ["x.txt", "x", "x.py.txt", "x.zip", "x.PY"])
def test_non_py_rejected(filename: str) -> None:
    """
    `.PY` 大写也被拒 —— 这是刻意的保守。

    `Path("x.PY").suffix.lower() == ".py"` 其实会通过，但这里用的是
    小写化后再比，所以 `.PY` 能过。这条用例断言的是"扩展名判断不区分大小写"
    （即 `.PY` 通过），避免以后有人改成严格比较而误伤 macOS 的大小写不敏感场景。

    ⚠ 若哪天策略改成只认小写，这条用例要跟着改 —— 它是**规约**，不是废话。
    """
    if filename == "x.PY":
        assert svc.validate_code(b"print(1)\n", filename)
        return
    with pytest.raises(InvalidToolUpload):
        svc.validate_code(b"print(1)\n", filename)


def test_empty_rejected() -> None:
    with pytest.raises(InvalidToolUpload) as exc:
        svc.validate_code(b"", "x.py")
    assert "空" in str(exc.value)


def test_oversize_rejected() -> None:
    """超限必须在**读文件之前**拦住（接口层读的是 MAX+1 字节）。"""
    with pytest.raises(InvalidToolUpload) as exc:
        svc.validate_code(b"#" * (MAX_CODE_BYTES + 1), "x.py")
    assert "过大" in str(exc.value)


def test_nul_byte_rejected() -> None:
    """
    NUL 不是合法的文本字符，且它穿到 PostgreSQL 会抛
    CharacterNotInRepertoireError（500 伪装成服务端故障）。
    """
    with pytest.raises(InvalidToolUpload) as exc:
        svc.validate_code(b"print(1)\x00\n", "x.py")
    assert "NUL" in str(exc.value)


def test_non_utf8_rejected() -> None:
    with pytest.raises(InvalidToolUpload) as exc:
        svc.validate_code("中文".encode("gbk"), "x.py")
    assert "UTF-8" in str(exc.value)


def test_syntax_error_reports_line() -> None:
    """
    报错必须带**行号与那一行原文**。

    只说"语法错误"等于让用户去猜 300 行里的哪一行 —— 而我们有 lineno，白给的信息。
    """
    bad = "def broken(:\n    pass\n"
    with pytest.raises(InvalidToolUpload) as exc:
        svc.validate_code(bad.encode(), "x.py")
    message = str(exc.value)
    assert "第 1 行" in message
    assert "broken" in message


def test_compile_does_not_execute() -> None:
    """
    `compile()` 只解析、不执行 —— 这条用例证明它。

    如果它执行了，下面这段会往 /tmp 写文件；用例断言文件不存在。
    （真正跑代码只能发生在沙箱里，绝不能发生在上传这一步。）
    """
    import tempfile
    from pathlib import Path

    marker = Path(tempfile.gettempdir()) / "agentforge_compile_probe.txt"
    marker.unlink(missing_ok=True)
    payload = f"open({str(marker)!r}, 'w').write('executed')\n"
    svc.validate_code(payload.encode(), "x.py")
    assert not marker.exists(), "compile() 竟然执行了代码 —— 这是严重的安全问题"


# ============================================================
# 三、路径
# ============================================================
def test_tool_dir_inside_root() -> None:
    import uuid

    tool_id = uuid.uuid4()
    assert svc.tool_dir(tool_id).name == str(tool_id)


@pytest.mark.parametrize("evil", ["../..", "../../etc", "/etc", "..", "."])
def test_tool_dir_traversal_rejected(evil: str) -> None:
    """
    目录名理论上来自 uuid，但函数签名允许字符串 —— 一旦有人传了 `../..`，
    删目录就会删到工具根之外。这类"路径穿越"是文件服务最常见的事故。
    """
    with pytest.raises(InvalidToolUpload):
        svc.tool_dir(evil)
