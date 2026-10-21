"""
工具沙箱隔离验收（六项断言）
=============================
这份脚本是「P2 沙箱」这个说法的**唯一证据**。没有它，
"我们有隔离"就只是一句设计意图 —— 而设计意图不能当验收。

跑的是什么：`app/mcp/sandbox.py` 里那份 `isolation_flags()` ——
**上线用的同一份参数**，不是这里另写一遍。若验收脚本自己拼一份参数，
"验收通过"只证明这份脚本是安全的，证明不了线上跑的是安全的（假绿）。

六项，各对应一类真实威胁：
    ① 非 root        → 读不到 /proc/1/environ（密钥）
    ② 无网络         → 连不上 db/redis，无法外传
    ③ 零密钥环境     → 环境变量里没有任何秘密
    ④ 根文件系统只读 → 改不了代码、留不下持久文件
    ⑤ 内存上限       → 死循环/大分配拖不垮宿主
    ⑥ 进程数上限     → fork 炸弹耗尽 PID 表

运行：
    uv run python -m scripts.tool_sandbox_verify
（需要先 `make sandbox` 构建镜像）
"""

from __future__ import annotations

import subprocess
import sys

from app.config import SENSITIVE_FIELDS, settings
from app.mcp import sandbox

PASS: list[str] = []
FAIL: list[str] = []


def _record(ok: bool, title: str, detail: str) -> None:
    (PASS if ok else FAIL).append(title)
    print(f"  {'[PASS]' if ok else '[FAIL]'} {title} —— {detail}")


def run_in_sandbox(code: str) -> subprocess.CompletedProcess:
    """
    用**线上同一份**隔离参数跑一段代码。

    `-c` 直接当参数传给镜像的 ENTRYPOINT（`python -E -s -B`），
    所以这里执行的是"容器内 python"，不是宿主。
    """
    args = [
        "docker", "run", "--rm",
        *sandbox.isolation_flags(),
        settings.tool_sandbox_image,
        "-c", code,
    ]
    return subprocess.run(  # noqa: S603 — 参数全部由本文件与常量拼成
        args, capture_output=True, text=True, timeout=120, env=sandbox.docker_env(),
    )


# ============================================================
# 各项断言
# ============================================================
def check_uid() -> None:
    """① 非 root：uid 必须是 10001。"""
    proc = run_in_sandbox("import os; print('UID', os.getuid())")
    uid = next((line.split()[1] for line in proc.stdout.splitlines() if line.startswith("UID")), "?")
    _record(uid == "10001", "非 root（uid）", f"uid={uid}（期望 10001）")


def check_network() -> None:
    """
    ② 无网络：连 1.1.1.1:53 必须失败，且 errno 是 101（Network is unreachable）。

    为什么专门断言 errno=101 而不是"只要失败就行"：
      `Connection refused`（111）意味着**包到了对方**，只是没人监听 ——
      那是"能联网但服务没开"，和"根本没有路"是两件事。
      只看"失败了"会把这两种情况混为一谈（标准答案见 docs/工具沙箱-…§1 题 1）。
    """
    code = (
        "import socket, errno\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), 2)\n"
        "    print('NET_OK')\n"
        "except OSError as e:\n"
        "    print('ERRNO', e.errno)\n"
    )
    proc = run_in_sandbox(code)
    errno_value = next(
        (line.split()[1] for line in proc.stdout.splitlines() if line.startswith("ERRNO")), "?"
    )
    _record(
        errno_value == str(101),
        "无网络（errno=101）",
        f"errno={errno_value}（101=Network unreachable；111=Connection refused 说明还能发出去）",
    )


def check_no_secrets() -> None:
    """
    ③ 零密钥：沙箱里的环境变量不能含**本项目任何一个敏感字段**。

    判据故意不用"关键字模糊匹配"：第一版用 `'KEY' in name` 判，
    结果 `GPG_KEY`（基础镜像自带的 Python 发布签名公钥，公开信息）和
    `PATH`（含子串 "PAT"）双双误报 —— 假红和假绿一样有害，都会让人不再信这个脚本。

    正解：拿 `SENSITIVE_FIELDS`（app/config.py 里那份**唯一出处**，与 repr 打码
    共用同一份清单）逐个大写比对。新增密钥字段时那份清单会自动跟着长，
    这里不用改 —— 这正是"唯一出处"的价值。

    这道防线靠的是 `docker run` **不继承宿主环境**（而不是我们主动清理）：
    前者不会漏，后者会。
    """
    proc = run_in_sandbox("import os; print('KEYS', ','.join(sorted(os.environ)))")
    keys_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith("KEYS")), "KEYS"
    )
    keys = {k.strip() for k in keys_line.removeprefix("KEYS").split(",") if k.strip()}

    expected_secrets = {name.upper() for name in SENSITIVE_FIELDS}
    leaked = sorted(keys & expected_secrets)

    # 附带一条体量断言：沙箱的 environ 应该只有基础镜像自带的那几个。
    # 数字不是重点，"少得可疑"才是 —— 一旦哪天它突然变成几十个，
    # 说明 docker run 那边被加上了 `-e` 或 `--env-file`。
    detail = f"{len(keys)} 个变量，敏感字段泄漏 {leaked or '无'}"
    _record(not leaked and len(keys) <= 12, "零密钥环境", detail)


def check_read_only_root() -> None:
    """④ 根文件系统只读：往 / 写文件必须 EROFS，而 /tmp 必须可写。"""
    code = (
        "try:\n"
        "    open('/probe', 'w').write('x')\n"
        "    print('ROOT_WRITABLE')\n"
        "except OSError:\n"
        "    print('ROOT_RO')\n"
        "open('/tmp/probe', 'w').write('x')\n"
        "print('TMP_OK')\n"
    )
    proc = run_in_sandbox(code)
    ok = "ROOT_RO" in proc.stdout and "TMP_OK" in proc.stdout
    _record(ok, "根只读 + /tmp 可写", "根不可写；/tmp（16MB 内存盘）可写")


def check_memory_limit() -> None:
    """
    ⑤ 内存上限：在 256MB 的容器里申请 400MB 必须失败。

    两种失败形态都算过，但机制不同、要能分辨：
      · 退出码 137 = 被内核 OOM killer 杀掉（cgroup 拦截）
      · MemoryError = 分配阶段就被拒
    判据取"**没有打印 MEM_OK**" —— 我们真正关心的是"400MB 没拿到"，
    至于是被谁拦下的，作为附加信息打印出来。
    """
    proc = run_in_sandbox("data = b'x' * (400 * 1024 * 1024); print('MEM_OK')")
    got = "MEM_OK" in proc.stdout
    if proc.returncode == 137:
        how = "被 OOM killer 杀掉（退出码 137）"
    elif "MemoryError" in proc.stderr:
        how = "分配阶段 MemoryError"
    else:
        how = f"退出码 {proc.returncode}"
    _record(not got, "内存上限（256m）", how)


def check_pids_limit() -> None:
    """
    ⑥ 进程数上限：开 200 个线程必须被拒（--pids-limit 64）。

    为什么用线程而不是子进程：cgroup 的 pids 统计的是**任务数**，
    线程与进程一样计入。用线程更轻、更快失败，不会在容器里留下残骸。
    """
    code = (
        "import threading, time\n"
        "started = 0\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        threading.Thread(target=time.sleep, args=(3,)).start()\n"
        "        started += 1\n"
        "    print('PIDS_NOT_LIMITED', started)\n"
        "except RuntimeError as e:\n"
        "    print('PIDS_BLOCKED', started, e)\n"
    )
    proc = run_in_sandbox(code)
    _record(
        "PIDS_BLOCKED" in proc.stdout,
        "进程数上限（64）",
        proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "无输出",
    )


def main() -> None:
    print("=" * 74)
    print("工具沙箱隔离验收（参数来自 app/mcp/sandbox.py，与线上同一份）")
    print(f"镜像：{settings.tool_sandbox_image}")
    print("=" * 74)

    ok, why = sandbox.image_exists()
    if not ok:
        print(f"  镜像不可用：{why}")
        sys.exit(1)

    print("[1/6] 身份与网络")
    check_uid()
    check_network()
    print("[2/6] 数据边界")
    check_no_secrets()
    check_read_only_root()
    print("[3/6] 资源配额")
    check_memory_limit()
    check_pids_limit()

    print("=" * 74)
    print(f"PASS={len(PASS)} FAIL={len(FAIL)}")
    if FAIL:
        print(f"未通过：{FAIL}")
        sys.exit(1)
    print("全部通过")
    print("=" * 74)


if __name__ == "__main__":
    main()
