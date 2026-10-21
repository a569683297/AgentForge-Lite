"""
工具服务器服务层
==================
职责四块，按"一次上传"的时间顺序排：

    ① 校验（纯函数，不碰 IO）—— 名字、扩展名、大小、编码、语法
    ② 落盘 —— 原子写入 <工具根>/<uuid>/server.py
    ③ 拉起 —— 交给 MCPManager（它内部走 docker run → 握手 → 发现 → 注册）
    ④ 持久化 —— 记状态与失败原因；重启时按 status 恢复

为什么校验放 service 层而不是接口层：
    接口层只负责"把 HTTP 的东西翻译成参数"。校验规则（比如以后允许 zip）
    会变，而 HTTP 的形状不该跟着变。更要紧的是 —— 单测可以**直接打纯函数**，
    不必为了验证"名字不合法"去起一个 FastAPI。

⚠ 本文件不拼任何 docker 参数：那是 app/mcp/sandbox.py 的事。
   "怎么跑"与"能不能跑"分开，改一个不会牵连另一个。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.db import async_session_factory
from app.core.logging import logger
from app.mcp import sandbox
from app.mcp.manager import get_manager
from app.models.tool_server import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_FAILED,
    TOOL_SERVER_STATUSES,
    ToolServer,
)

# ---- 名字规则（唯一出处）----
# 这个名字不只是一个标签，它同时是：
#   ① 工具名前缀（命名空间）→ 必须是合法的工具名片段
#   ② 注册表的 source 值   → 必须能唯一定位一台 server
# 所以限制得比"显示名"严得多：小写、下划线、不带点号与短横线。
# 为什么不用短横线：工具名会进 OpenAI function calling 的 name 字段，
# 各家对允许字符的宽容度不一致，下划线是最安全的交集。
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

# 保留名：这些 source 已经被系统占用，用户不能拿来当自己的名字。
# ⚠ 这不是洁癖 —— `unregister_source(source)` 是**按 source 整批摘工具**的。
#   如果允许用户叫 `local`，删掉他的工具时会把项目内置工具一起摘干净；
#   叫 `inventory` 则会把 D26 那台 server 的工具连带摘掉。
RESERVED_NAMES: frozenset[str] = frozenset({"local", "inventory", "harness"})

# 单文件上限。256KB 足够写一个复杂工具，又小到"不小心传了个数据集"能被拦住。
MAX_CODE_BYTES = 256 * 1024

ALLOWED_SUFFIX = ".py"


# ============================================================
# 异常：带 HTTP 状态码，接口层直接映射，不做二次判断
# ============================================================
class ToolServerError(Exception):
    """本模块所有可预期错误的基类。`status_code` 由接口层直接采用。"""

    status_code = 400


class InvalidToolUpload(ToolServerError):
    """上传内容不合法（400）—— 用户换一个文件/改个名字就能解决。"""

    status_code = 400


class ToolNameConflict(ToolServerError):
    """名字已被占用（409）—— 与文档去重同一个语义：不是错误，是状态冲突。"""

    status_code = 409


class ToolNotFound(ToolServerError):
    """没有这条记录（404）。"""

    status_code = 404


class SandboxUnavailable(ToolServerError):
    """沙箱没开或环境不满足（503）—— 不是用户的错，是部署没配好。"""

    status_code = 503


# ============================================================
# 一、校验（纯函数，单测直接打）
# ============================================================
def validate_name(raw: str) -> str:
    """
    规范化 + 校验工具名。

    "规范化"在这里是**转小写 + 去空白**，而不是静默截断或替换：
    `MyTool` → `mytool` 是可接受的（用户意图明显），
    但 `my-tool` → `my_tool` 就不做 —— 那等于替用户改主意，
    而且他下次按 `my-tool` 找不到东西。宁可直接拒绝并说清规则。
    """
    name = (raw or "").strip().lower()
    if not NAME_RE.match(name):
        raise InvalidToolUpload(
            "工具名不合法：必须是 3-32 位，小写字母开头，只含小写字母/数字/下划线"
            f"（收到 {raw!r}）"
        )
    if name in RESERVED_NAMES:
        raise InvalidToolUpload(
            f"`{name}` 是系统保留名（{sorted(RESERVED_NAMES)}）。"
            "换一个名字 —— 这个值会被用作工具名前缀，占用它会导致系统工具被误删"
        )
    return name


def validate_code(content: bytes, filename: str) -> str:
    """
    校验上传的代码，返回解码后的源码。

    顺序是刻意的：**先便宜后昂贵** —— 扩展名/大小/编码都是 O(1) 或 O(n) 的检查，
    语法编译是 O(n) 但常数大得多。让明显的错误在便宜的那一步就返回。

    ⚠ `compile()` 只做**解析**，不执行任何东西 —— 它不会 import、不会跑顶层代码。
      这既不是安全边界（真正的边界是沙箱），也不是"代码没毛病"的证明，
      它只是保证"这段东西至少是个语法正确的 Python 文件"，
      免得用户上传一个日志文件，然后在握手超时里等 20 秒。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix != ALLOWED_SUFFIX:
        raise InvalidToolUpload(
            f"只接受 {ALLOWED_SUFFIX} 文件（收到 {suffix or '无扩展名'}）。"
            "多文件/zip 形态尚未开放"
        )

    if not content:
        raise InvalidToolUpload("文件是空的")

    if len(content) > MAX_CODE_BYTES:
        raise InvalidToolUpload(
            f"文件过大：{len(content)} 字节，上限 {MAX_CODE_BYTES} 字节（256KB）"
        )

    # NUL 字节在源码里非法，而且它一路穿到 PostgreSQL 会抛
    # CharacterNotInRepertoireError（app/main.py 里那个路径中间件防的是同一类问题）。
    if b"\x00" in content:
        raise InvalidToolUpload("文件含 NUL 字节，不是合法的文本源码")

    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InvalidToolUpload(f"文件必须是 UTF-8 编码：{e}") from e

    try:
        compile(source, filename, "exec")
    except SyntaxError as e:
        raise InvalidToolUpload(
            f"语法错误：第 {e.lineno} 行 `{(e.text or '').strip()}` —— {e.msg}"
        ) from e

    return source


# ============================================================
# 二、磁盘
# ============================================================
def tool_dir(tool_id: uuid.UUID | str) -> Path:
    """
    某个工具的代码目录，**并且校验它确实落在工具根之内**。

    为什么要做这个校验：目录名来自主键（uuid），理论上安全；
    但这个函数也可能被别处以字符串形式调用。一旦有人传了 `../..`，
    删目录时就会删到工具根之外。这类"路径穿越"（path traversal）在
    文件服务里是最常见的一类事故，成本极低的一道断言值得加。
    """
    root = sandbox.code_root().resolve()
    target = (root / str(tool_id)).resolve()
    if target == root or root not in target.parents:
        raise InvalidToolUpload(f"非法工具目录：{tool_id!r}")
    return target


def save_code(tool_id: uuid.UUID, source: str) -> tuple[str, str, int]:
    """
    写代码文件，返回 (entry_file 相对路径, sha256, 字节数)。

    为什么先写临时文件再 `replace`：`replace` 在同一文件系统内是**原子**的。
    直接覆盖原文件的话，一个正在被容器读的旧文件会在半截上被换掉 ——
    容器要么读到半份代码，要么读不到。原子替换让"旧"和"新"之间没有中间态。

    sha256 存在的理由不是校验完整性（那有 HTTPS 与 docker 管），
    而是回答"容器里跑的到底是不是我刚传的那份"：
    同名重传时，代码变了但工具名没变，日志里唯一的线索就是这个指纹。
    """
    directory = tool_dir(tool_id)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / "server.py"
    tmp = directory / ".server.py.tmp"
    tmp.write_text(source, encoding="utf-8")
    tmp.replace(final)

    data = final.read_bytes()
    return f"{tool_id}/server.py", hashlib.sha256(data).hexdigest(), len(data)


def remove_code(tool_id: uuid.UUID) -> None:
    """删掉一个工具的代码目录（`ignore_errors` 只容忍"本来就不存在"）。"""
    shutil.rmtree(tool_dir(tool_id), ignore_errors=True)


# ============================================================
# 三、运行态（拉起 / 停掉 / 合并状态）
# ============================================================
def _runtime(name: str) -> dict[str, Any]:
    """从 MCPManager 取某台 server 的**此刻**状态；没有就返回空字典。"""
    for item in get_manager().status():
        if item.get("name") == name:
            return item
    return {}


def serialize(row: ToolServer) -> dict[str, Any]:
    """
    库里的持久事实 + 管理器的运行事实 → 一个给前端的对象。

    两块都要，而且**故意不合并成一个 status**：
      · `status`（库）回答"它该不该跑"
      · `connected`（运行态）回答"它此刻在不在跑"
    两者不一致是**要看见的信号**：容器崩了但库里还是 active，
    说明"该跑却没跑"，用户该点重连。把它折叠成一个字段就看不见了。
    """
    runtime = _runtime(row.name)
    tools = runtime.get("tools") or []
    return {
        "id": str(row.id),
        "name": row.name,
        "display_name": row.display_name,
        "description": row.description or "",
        "status": row.status,
        "connected": bool(runtime.get("connected")),
        "tools": tools,
        "tool_count": len(tools),
        "last_error": row.last_error or runtime.get("error"),
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _save_row(row: ToolServer) -> None:
    """把内存里改过的行写回库（独立 session：调用方不关心事务边界）。"""
    async with async_session_factory() as session:
        await session.merge(row)
        await session.commit()


async def _start(row: ToolServer) -> None:
    """
    拉起（或重启）一台工具 server，并把结果写回库里。

    ⚠ 这里**吞掉所有异常**，只落状态：一台用户工具起不来，
      不该让"上传"这个动作变成 500。它与 D27 lifespan 里那句
      "外部 server 起不来不能拖死应用"是同一条原则，只是换到了用户维度。
    """
    spec = sandbox.build_spec(str(row.id), row.name, row.entry_file)
    try:
        # 用 *_held 而不是 add_server：这条路径跑在 HTTP 请求任务里，
        # 必须让接入发生在一个长驻任务中（否则请求结束会把传输一起带走）。
        # 详见 MCPManager.add_server_held 的 docstring。
        result = await get_manager().add_server_held(spec)
    except Exception as e:  # noqa: BLE001 — 边界：任何异常都要变成"一条失败记录"
        result = {"connected": False, "error": f"{type(e).__name__}: {e}"}

    if result.get("connected"):
        row.status = STATUS_ACTIVE
        row.last_error = None
        logger.info(
            "工具 server 已接入 name=%s 工具数=%s", row.name, len(result.get("tools") or [])
        )
    else:
        row.status = STATUS_FAILED
        # 截断：错误里可能带整段 stderr。库里存得下，但前端展示会糊成一团，
        # 完整原文在 MCPClient 写的 stderr 日志文件里（路径由 manager 的 status 给出）。
        row.last_error = (result.get("error") or "未知原因")[:1000]
        logger.warning("工具 server 接入失败 name=%s 原因=%s", row.name, row.last_error)

    await _save_row(row)


# ============================================================
# 四、对外用例
# ============================================================
async def create(
    *,
    name: str,
    display_name: str,
    description: str,
    filename: str,
    content: bytes,
    owner_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """
    上传一台工具 server：校验 → 落盘 → 入库 → 拉起。

    失败语义（三层，别混）：
      · 校验不过      → 抛 InvalidToolUpload（400），**什么都没留下**
      · 名字被占      → 抛 ToolNameConflict（409），**什么都没留下**
      · 拉起失败      → 正常返回对象，`connected=false` + `last_error`
        为什么这一层不抛：代码存进去了、记录建好了，用户能看见它、能改、能删。
        把它做成 500 会让前端以为"上传没成功"，于是重传 → 又一条失败记录。
    """
    if not settings.tool_sandbox_enabled:
        raise SandboxUnavailable(
            "工具沙箱未启用。设置 TOOL_SANDBOX_ENABLED=true 并确保 docker socket 可用"
        )

    normalized = validate_name(name)
    source = validate_code(content, filename)

    ok, why = await asyncio.to_thread(sandbox.image_exists)
    if not ok:
        raise SandboxUnavailable(why)

    # 先查一次给出干净的 409（并发下仍可能撞，靠下面的 IntegrityError 兜底）
    async with async_session_factory() as session:
        exists = (
            await session.execute(select(ToolServer.id).where(ToolServer.name == normalized))
        ).first()
    if exists:
        raise ToolNameConflict(f"工具名 `{normalized}` 已被占用，换一个名字")

    tool_id = uuid.uuid4()
    entry_file, digest, size = await asyncio.to_thread(save_code, tool_id, source)

    # ★ 在写库之前先确认"沙箱真的看得到这份文件"。
    #   放在这里而不是等 _start 失败的原因：卷映射错了是**部署问题**，
    #   不是用户的代码有问题。让它以 503 + 明确原因返回，而不是建一条
    #   status=failed 的记录让用户以为是自己写得不对（并且还占掉了这个名字）。
    visible, reason = await asyncio.to_thread(sandbox.can_see, entry_file)
    if not visible:
        await asyncio.to_thread(remove_code, tool_id)
        raise SandboxUnavailable(reason)

    row = ToolServer(
        id=tool_id,
        name=normalized,
        # 只作审计：见 ToolServer.owner_id 的注释 —— 工具是管理员能力，
        # 因为 ToolRegistry 是**进程级**的，LLM 看到的是全局工具表
        owner_id=owner_id,
        display_name=(display_name or "").strip() or normalized,
        description=(description or "").strip(),
        entry_file=entry_file,
        sha256=digest,
        size_bytes=size,
        status=STATUS_ACTIVE,
    )
    try:
        async with async_session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
    except IntegrityError as e:
        # 并发上传同名：唯一约束是最后一道闸。把半截产物清掉再报冲突，
        # 否则会留下一份"有代码、没记录"的孤儿目录（永远不会被 GC）。
        await asyncio.to_thread(remove_code, tool_id)
        raise ToolNameConflict(f"工具名 `{normalized}` 已被占用，换一个名字") from e

    await _start(row)
    return serialize(row)


async def list_all() -> list[dict[str, Any]]:
    """全部工具 server（含已停用的），新上传的排前面。"""
    async with async_session_factory() as session:
        rows = (
            await session.execute(select(ToolServer).order_by(ToolServer.created_at.desc()))
        ).scalars().all()
    return [serialize(row) for row in rows]


async def _get(tool_id: uuid.UUID) -> ToolServer:
    async with async_session_factory() as session:
        row = await session.get(ToolServer, tool_id)
    if row is None:
        raise ToolNotFound("工具 server 不存在")
    return row


async def reconnect(tool_id: uuid.UUID) -> dict[str, Any]:
    """手动重连（代码改过、容器崩了、开机没起来 —— 都用这个）。"""
    row = await _get(tool_id)
    if not sandbox.docker_available():
        raise SandboxUnavailable("docker CLI 不可用")
    await _start(row)
    return serialize(row)


async def set_status(tool_id: uuid.UUID, status: str) -> dict[str, Any]:
    """启用 / 停用。停用 = 断开并把工具从注册表摘掉（D27 的 unregister_source）。"""
    if status not in TOOL_SERVER_STATUSES:
        raise InvalidToolUpload(f"status 只能是 {TOOL_SERVER_STATUSES} 之一")
    if status == STATUS_FAILED:
        # failed 是"结果"，不是"意图" —— 不允许用户手动设成它。
        raise InvalidToolUpload("failed 是拉起失败的结果，不能手动设置；请用 active 重试")

    row = await _get(tool_id)
    if status == STATUS_ACTIVE:
        await _start(row)
    else:
        await get_manager().remove_server(row.name)
        row.status = STATUS_DISABLED
        row.last_error = None
        await _save_row(row)
    return serialize(row)


async def call_tool(tool_id: uuid.UUID, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    试跑一台已上传 server 的某个工具（走**线上同一条**调用路径）。

    为什么必须先校验归属：
      这个接口接的是 `execute_tool(name, args)` —— 它对注册表里**任何**工具都生效，
      包括内置的 `search_documents` 与 `query_inventory`。
      不校验的话，它就变成了"绕过 Agent 直接调任意系统工具"的后门。
      校验用 `get_tool_source(full) == row.name`：注册表才是出身的唯一出处，
      按名字前缀猜是不行的（前缀是配置出来的，不是事实）。

    返回的是工具的**原始字符串输出**（与 LLM 看到的完全一致），
    不做任何美化 —— 用户要判断的是"工具回了什么"，不是我们包装成什么样。
    """
    from app.tools.registry import execute_tool, get_tool_source   # 局部 import：避免循环依赖

    row = await _get(tool_id)
    name = (tool or "").strip()
    if not name:
        raise InvalidToolUpload("tool 不能为空")

    full = name if name.startswith(f"{row.name}_") else f"{row.name}_{name}"
    if get_tool_source(full) != row.name:
        raise ToolNotFound(
            f"工具 `{name}` 不存在或不属于「{row.display_name}」。"
            f"可用工具：{_runtime(row.name).get('tools') or '（该 server 当前未连接）'}"
        )

    output = await execute_tool(full, arguments)
    return {"tool": full, "output": output}


async def delete(tool_id: uuid.UUID) -> None:
    """
    删掉一台工具 server：先摘工具，再删文件，最后删记录。

    顺序的理由：`remove_server` 会调 `unregister_source`，让 LLM **立刻**
    看不到这些工具。若反过来先删记录，中间那段时间里工具还在注册表里，
    但已经没有任何记录能解释它们从哪来 —— 调用会失败，而排障无据可查。
    """
    row = await _get(tool_id)
    await get_manager().remove_server(row.name)
    await asyncio.to_thread(remove_code, row.id)
    async with async_session_factory() as session:
        await session.delete(await session.get(ToolServer, tool_id))
        await session.commit()
    logger.info("工具 server 已删除 name=%s", row.name)


async def restore_active() -> dict[str, Any]:
    """
    启动时按库里的 `status=active` 重新拉起（**重启不丢**的关键一步）。

    这也是这张表存在的主要理由：容器是 `--rm` 的一次性进程，
    api 一重启它们全没了，而界面上只会显示"空列表"——看起来像从没传过。

    三条防御：
      ① 沙箱没开 → 直接返回，不报错
      ② 代码文件不见了（卷被清过）→ 标 failed 并写清原因，而不是反复重试
      ③ 单台起不来 → 只影响它自己，continue 到下一台
    """
    if not settings.tool_sandbox_enabled:
        return {"restored": 0, "failed": 0, "skipped": "sandbox disabled"}

    ok, why = await asyncio.to_thread(sandbox.image_exists)
    if not ok:
        logger.warning("工具沙箱不可用，跳过恢复：%s", why)
        return {"restored": 0, "failed": 0, "skipped": why}

    async with async_session_factory() as session:
        rows = (
            await session.execute(select(ToolServer).where(ToolServer.status == STATUS_ACTIVE))
        ).scalars().all()

    restored = failed = 0
    for row in rows:
        if not (sandbox.code_root() / row.entry_file).exists():
            row.status = STATUS_FAILED
            row.last_error = f"代码文件缺失：{row.entry_file}（工具卷被清理过？）"
            await _save_row(row)
            failed += 1
            continue
        await _start(row)
        restored += row.status == STATUS_ACTIVE
        failed += row.status != STATUS_ACTIVE

    if rows:
        logger.info("工具 server 恢复完成 成功=%d 失败=%d", restored, failed)
    return {"restored": restored, "failed": failed, "skipped": None}
