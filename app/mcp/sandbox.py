"""
工具沙箱的「启动说明书」生成器
================================
这个文件只做一件事：把一份**用户上传的工具**翻译成一条 `docker run` 命令，
包装成 `MCPServerSpec` 交给 D27 已有的 `MCPManager`。

为什么翻译完就不用管了（这是整个方案的关键）：
    `MCPClient` 只认 `MCPServerSpec(command, args, ...)` —— 它不关心
    `command` 是 `python` 还是 `docker`。所以：
        · D27 时代：command=python  args=[inventory_server.py]
        · 现在：    command=docker   args=[run -i --rm --network none ...]
    两条路径在 `MCPClient` 看来是同一件事，**Agent 引擎与注册表零改动**。

一次调用的三跳边界（排障时要按这个顺序查）：
    api 进程 ──pipe──> docker CLI 进程 ──unix socket──> dockerd ──pipe──> 沙箱内 python
    第 1 跳断了 → FileNotFoundError（docker 不在 PATH）
    第 2 跳断了 → `Cannot connect to the Docker daemon`（socket 没挂进来）
    第 3 跳断了 → 容器起不来（镜像缺失/代码语法错），stderr 落在 MCPClient 的 stderr 日志里

⚠ 本文件**不决定**"能不能跑"（那是 service 层的校验），只负责"怎么跑"。
   职责分开的理由：校验规则会随安全要求变，而隔离参数不该跟着动。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.core.logging import logger
from app.mcp.client import MCPServerSpec

# 项目根：app/mcp/sandbox.py → app/mcp → app → 根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 沙箱内的挂载点与解释器路径。**必须与 sandbox/Dockerfile 保持一致**。
SANDBOX_MOUNT = "/tool"
SANDBOX_UID = "10001:10001"

# 容器标签：孤儿清理靠它精确匹配"我们起的"容器，绝不用名字前缀猜。
LABEL_KEY = "agentforge.tool"

# 给 docker CLI 进程的最小环境。
# ⚠ 为什么不直接 `{**os.environ}`：与 D27 `_base_env()` 同一个坑 ——
#   宿主环境里的同名变量会污染子进程行为。这里连 PATH 都自己拼，
#   为的是"能不能找到 docker"不依赖 api 进程碰巧继承了什么样的 PATH。
#
# PATH 里**必须带上 docker 实际所在目录**：macOS 上 Docker Desktop 装在
# /usr/local/bin，而 brew 装法在 /opt/homebrew/bin。写死一个会在另一种机器上
# 报 FileNotFoundError —— 而那个报错看起来像"沙箱坏了"，其实是找不到客户端。


def docker_bin() -> str:
    """docker 可执行文件的路径（找不到就退回字面量 "docker"，让报错说人话）。"""
    return shutil.which("docker") or "docker"


def docker_env() -> dict[str, str]:
    """docker CLI 子进程的环境（白名单，且自动带上 docker 所在目录）。"""
    dirs = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    found = shutil.which("docker")
    if found:
        parent = str(Path(found).parent)
        if parent not in dirs:
            dirs.insert(0, parent)
    return {
        "PATH": ":".join(dirs),
        "HOME": "/tmp",
        # 显式写死 socket 路径：默认值虽然是它，但写出来之后，
        # 一旦挂载点变了，报错会直接指向这一行，而不是让人去翻 docker 的默认行为。
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }


def in_container() -> bool:
    """
    当前进程是否跑在容器里。

    为什么要判这个（本方案最容易踩的一处坑）：
      `docker run -v` 的**左边由宿主 daemon 解析**，不是 api 进程眼里的路径。
      · api 在容器里：写 `/app/tool_code` → 宿主上没这个目录 → 沙箱看到空目录
      · api 在本机 ：写宿主机绝对路径 → 正确
    两种形态必须走两条分支，不能用一个值覆盖两种情况。

    判据用 `/.dockerenv`（Docker 一定会在容器根目录建这个文件），
    而不是"猜路径像不像容器路径" —— 猜是错的来源。
    """
    return Path("/.dockerenv").exists()


def code_root() -> Path:
    """
    工具的落盘根目录（**api 进程视角**的绝对路径）。

    配置里可能是相对路径（dev 形态的 data/tool_servers），
    相对路径一律相对项目根解析 —— 不相对 cwd，否则从别的目录启动就写错地方。
    """
    raw = Path(settings.tool_code_dir).expanduser()
    return raw if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()


def mount_ref() -> str:
    """
    拼 `-v` 的左值：给它一段"到处都能解析"的引用。

    · 容器形态 → **命名卷**（`tool_code:/tool:ro`）。名字由 docker 解析，
      与宿主机路径无关 —— 这正是绕开上面那个坑的办法。
    · 本机形态 → 宿主机绝对路径（daemon 就是本机 daemon，路径天然成立）。

    `:ro` 是硬要求：容器里能改自己的代码 = 用户下次调用跑的是被篡改的逻辑。
    """
    if in_container():
        return f"{settings.tool_code_volume}:{SANDBOX_MOUNT}:ro"
    return f"{code_root()}:{SANDBOX_MOUNT}:ro"


def isolation_flags() -> list[str]:
    """
    隔离参数清单（**唯一出处**）。

    单独抽出来是为了让验收脚本（scripts/tool_sandbox_verify.py）跑的是
    **同一份**参数 —— 否则"验收通过"只证明验收脚本自己写的那串参数是安全的，
    而真正上线用的可能是另一串。这类"验的和用的不是一回事"是最典型的假绿。

    每一条挡什么，见 docs/工具沙箱-隔离原理与自测答案-2026-10-20.md §3。
    """
    return [
        "--network", "none",              # 独立 NET namespace，且只有 lo，没有通往任何地方的路由
        "--read-only",                    # 根文件系统只读（MNT namespace）
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",   # 唯一可写处：内存盘、不可执行、16MB 上限
        "--user", SANDBOX_UID,            # 非 root → 读不到 /proc/1/environ
        "--cap-drop", "ALL",              # 丢弃全部内核特权
        "--security-opt", "no-new-privileges",   # 不允许通过 setuid 提权
        "--memory", settings.tool_sandbox_memory,      # cgroup 内存上限
        "--cpus", settings.tool_sandbox_cpus,          # cgroup CPU 时间配额
        "--pids-limit", str(settings.tool_sandbox_pids_limit),   # 防 fork 炸弹
    ]


def container_name(tool_id: str) -> str:
    """容器名：`docker ps` 里一眼能看出是谁。取 uuid 前 8 位足够区分。"""
    return f"agentforge-tool-{str(tool_id)[:8]}"


def run_args(tool_id: str, entry_file: str) -> list[str]:
    """
    完整的 `docker run` 参数（不含 command 本身）。

    `-i` 是命根子：它保持 STDIN 打开，docker CLI 才会把我们的字节转进容器。
    **不加 `-t`**：TTY 会引入行尾转换与回显，把 JSON-RPC 报文弄坏
    （症状是握手超时，而容器日志里一切正常 —— 很难查）。

    `entry_file` 是相对工具根的路径（`<uuid>/server.py`），
    拼成容器内路径 `/tool/<uuid>/server.py`。
    """
    return [
        "run", "-i", "--rm",
        "--name", container_name(tool_id),
        "--label", f"{LABEL_KEY}={tool_id}",
        *isolation_flags(),
        "-v", mount_ref(),
        settings.tool_sandbox_image,
        f"{SANDBOX_MOUNT}/{entry_file}",
    ]


def build_spec(tool_id: str, name: str, entry_file: str) -> MCPServerSpec:
    """
    组装成 MCPManager 能吃的规格。

    · `namespace=name`：远端工具 `text_stats` → 注册成全名 `demo_text_stats`。
      必须加前缀 —— 两台工具服务都可能有 `search` 这种泛名，
      而 `register` 撞名会 `raise ValueError`（启动即炸）。
    · `call_timeout_s=30`：与 D27 一致。它防的不是"慢"，是"容器活着但不应答"
      —— 那种情况会挂住整轮对话。
    """
    docker = docker_bin()
    return MCPServerSpec(
        name=name,
        command=docker,
        args=run_args(tool_id, entry_file),
        env=docker_env(),   # 显式白名单，不透传宿主环境
        namespace=name,
        cwd=None,                # 沙箱不依赖 api 的工作目录
        call_timeout_s=30.0,
    )


# ============================================================
# 预检与清扫（都是"提前把话说清楚"的小工具）
# ============================================================
def docker_available() -> bool:
    """docker CLI 在不在。不在的话后面每一条都会以难懂的方式失败，先问清楚。"""
    return shutil.which("docker") is not None


def image_exists() -> tuple[bool, str]:
    """
    沙箱镜像在不在。**启动时先查**，不要等第一次 `docker run`。

    为什么：镜像不存在时 `docker run` 会去 pull，表现为"几分钟没有任何输出"。
    用户看到的是转圈，日志里是空的 —— 而真正的原因一句话就能说清：
    "请先执行 make sandbox"。能一句话说清的事，不该让人等五分钟。
    """
    if not docker_available():
        return False, "docker CLI 不可用（镜像未检查）"
    proc = subprocess.run(  # noqa: S603 — 参数是我们自己拼的常量，无用户输入
        ["docker", "image", "inspect", settings.tool_sandbox_image],
        capture_output=True, text=True, timeout=20, env=docker_env(),
    )
    if proc.returncode != 0:
        return False, (
            f"沙箱镜像 {settings.tool_sandbox_image} 不存在。"
            f"请先执行 `make sandbox` 构建（首次需要联网装 mcp SDK）"
        )
    return True, ""


def sweep_orphans() -> list[str]:
    """
    清掉上一次残留的沙箱容器。

    为什么会有残留：api 被 `kill -9` 时，stdin 不会优雅关闭 ——
    容器读不到 EOF，就一直等在自己的 stdin 上（它是"归宿主管"的进程，
    见 D26 的结论）。不扫的话，每崩一次就多一具尸体，最终耗尽名字/内存。

    只删带我们标签的容器：`docker rm -f` 是破坏性操作，
    范围必须由标签**精确界定**，绝不能按名字前缀模糊匹配。
    """
    if not docker_available():
        return []
    listed = subprocess.run(  # noqa: S603
        ["docker", "ps", "-aq", "--filter", f"label={LABEL_KEY}"],
        capture_output=True, text=True, timeout=20, env=docker_env(),
    )
    ids = listed.stdout.split()
    if not ids:
        return []
    subprocess.run(  # noqa: S603
        ["docker", "rm", "-f", *ids],
        capture_output=True, text=True, timeout=60, env=docker_env(),
    )
    logger.warning("清理残留沙箱容器 %d 个：%s", len(ids), ids)
    return ids


def can_see(entry_file: str) -> tuple[bool, str]:
    """
    在**真正的沙箱容器里**探一下：能不能看到这份代码文件。

    为什么必须探（不是多余的一道）：
      卷映射是"api 进程的视角"与"宿主 daemon 的视角"之间唯一的接缝。
      它错了的时候，症状极其误导 —— 容器能起来、握手立刻断
      （`MCPError: Connection closed`），因为 python 找不到脚本就直接退出了。
      而"找不到脚本"和"用户代码里 import 挂了"在客户端看来一模一样。
      探一下 path 是否存在，就能把这两类彻底分开，并把原因说成人话。

    实测出处：2026-10-20 首次端到端上传就撞上了这个坑（compose 会给卷加项目
    前缀，容器内却按裸名挂载 → docker 静默新建了一个空卷）。所以这条检查
    不是防御性编程，是**已经发生过一次**的事故复盘。

    只在"上传"时调用（每次约 0.4s），不进热路径。
    """
    if not docker_available():
        return False, "docker CLI 不可用"
    # entry_file 由我们自己拼（<uuid>/server.py），不含用户输入。
    code = (
        "import os, sys; "
        f"sys.exit(0 if os.path.exists('{SANDBOX_MOUNT}/{entry_file}') else 3)"
    )
    proc = subprocess.run(  # noqa: S603
        ["docker", "run", "--rm", *isolation_flags(), "-v", mount_ref(),
         settings.tool_sandbox_image, "-c", code],
        capture_output=True, text=True, timeout=60, env=docker_env(),
    )
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 3:
        return False, (
            "沙箱看不到代码文件：卷映射不正确。"
            "检查 docker-compose.yml 的 volumes.tool_code.name 与 "
            "TOOL_CODE_VOLUME 是否一致（compose 默认会加项目前缀）"
        )
    return False, f"沙箱启动失败：{(proc.stderr or '').strip()[:300]}"
