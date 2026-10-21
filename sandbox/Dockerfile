# ============================================================
# 用户上传工具的沙箱基础镜像（增量 0）
# ============================================================
# 用途：api 用 `docker run -i --rm ...` 起的每一个「用户上传的 MCP Server」，
#      跑的都是这个镜像里的一份 python。
#
# 为什么单独做一个镜像，不复用 api 镜像：
#   ① **暴露面**：api 镜像里有 app/ 全部源码、.venv、tests/、metrics.yaml。
#      沙箱只需要一个 python + mcp SDK —— 不需要的东西就不该出现在它面前。
#   ② **依赖可控**：沙箱装的包越少，能被打的主意越少。api 镜像里有 asyncpg、
#      langfuse、fastembed……每一个都是一条潜在入口。
#   ③ **升级解耦**：换 api 的依赖不该顺手改掉沙箱的运行时。
#
# 构建：`make sandbox`
#   = docker build -f sandbox/Dockerfile -t agentforge-tool-sandbox:local sandbox/
#   ⚠ 构建上下文故意是 sandbox/ 而不是项目根：只拷这一个目录，
#     不给构建过程任何机会把 .env / app/ 带进镜像。
# ============================================================

FROM python:3.12-slim

# PYTHONUNBUFFERED：stdout 不缓冲。
#   ★ 这一条对本镜像比对 api 更要紧：MCP 走 stdio，响应必须**立刻**写出去。
#     被缓冲住的话，调用方要等缓冲区满或进程退出才拿到结果 —— 表现为"调用超时"，
#     而工具其实早就执行完了。这是最难查的一类慢。
# PYTHONDONTWRITEBYTECODE：不写 .pyc。运行时根文件系统是只读的，
#   写了也只会到处报 EROFS。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# MCP SDK —— 工具侧唯一的硬依赖。
# 版本**锁死**而不是 `>=`：沙箱与宿主之间只有一份契约，就是 MCP 协议报文。
# 让它随上游漂 = 某天工具突然握手失败，而两边代码都没人改过。
# 升级方式：改这一行 + pyproject.toml，重跑 `make sandbox`。
RUN pip install --no-cache-dir "mcp==2.2.0"

# 非 root 用户（uid 10001 是本项目约定的沙箱 uid）。
# ★ 这一行是「密钥防线」的另一半，不是可有可无的整洁：
#   api 容器用 env_file 注入了全部密钥（DeepSeek key / Langfuse secret / Harness PAT）。
#   子进程即使拿不到环境变量，只要 uid 与 api 相同（root），照样能读
#   /proc/1/environ 把密钥原样取出来。uid 一换，这条路就断了。
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin tool

# HOME 指向 /tmp：运行时根文件系统只读，若 HOME 落在 /home/tool，
# 任何库尝试写 HOME 都会 EROFS。/tmp 由 api 侧用 --tmpfs 挂成可写。
ENV HOME=/tmp

USER 10001
WORKDIR /tool

# 入口固定成 python：api 侧只传「脚本路径 + 参数」，不传解释器 ——
# 这样"用户能执行什么"被收敛到"一段 python 代码"，而不是"任意命令"。
#
# ★ 用 -E -s -B，**不是 -I**：
#   -E 忽略 PYTHON* 环境变量、-s 不加载 user site、-B 不写 .pyc，隔离目的都达到了。
#   但 -I 从 Python 3.11 起**隐含 -P**（不把脚本所在目录放进 sys.path），
#   而"多文件工具"要靠同目录 import 才能工作（增量 6 的 zip 形态）。
#   实测（3.13.15）：`-I /tmp/ptest/x.py` 的 sys.path 里没有 /tmp/ptest；`-E -s` 有。
#   现在选 -E -s -B，是为了增量 6 不用回头改镜像。
ENTRYPOINT ["python", "-E", "-s", "-B"]
