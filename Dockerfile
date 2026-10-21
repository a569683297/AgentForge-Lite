# ============================================================
# AgentForge API 镜像（D34）
# ============================================================
# 构建：docker build -t agentforge-api:local .
# 由 docker-compose.yml 里 api.build 自动调用（context = 项目根）
#
# 为什么基础镜像选 python:3.12-slim：
#   ① pyproject.toml 写的是 requires-python = ">=3.12" → 用 3.12
#   ② slim 是 glibc 的（Debian），onnxruntime / asyncpg 这些带 C 扩展的包
#      在这上面**有现成 wheel**，不用现场编译；alpine 走 musl，常要自己编译
# ============================================================

# pin 到与本机一致的 uv 版本（`uv --version` = 0.12.4），保证本机/镜像行为一致
FROM python:3.12-slim

# ---------- 1. 基础环境变量 ----------
# PYTHONUNBUFFERED：日志**立刻**刷出来。不加的话 Python 会缓冲 stdout，
#   `docker compose logs -f api` 会"半天不输出、然后一次性全出来"。
# PYTHONDONTWRITEBYTECODE：不写 .pyc —— 容器是一次性的，写了也没人受益。
# UV_LINK_MODE=copy：uv 默认用硬链接装包，容器里缓存与 venv 常跨文件系统，
#   不改这个会刷一堆 "failed to hardlink" 警告（功能正常，但很吵）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# uv 从官方镜像里**只拷二进制**（比 pip install uv 快、且不引入 pip 的依赖树）
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /uvx /usr/local/bin/

# ---------- docker CLI（工具沙箱，增量 0）----------
# 用户上传的 MCP Server 要跑在**同级兄弟容器**里，所以 api 需要能执行 `docker run`。
# ★ 只拷**客户端二进制**，不装 dockerd：真正干活的是宿主机上的 daemon，
#   我们通过 /var/run/docker.sock 连它（compose 里挂进来）。
#   这个二进制是静态编译的 Go 程序，在 slim 镜像里直接能跑，不拉依赖树。
# ⚠ 不拷它的症状：`docker run` 以 FileNotFoundError 失败，而这条报错出现在
#   "用户上传工具"的链路上，极难联想到是基础镜像缺了个可执行文件。
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# ---------- 2. 先装依赖（★ 顺序是刻意的）----------
# Docker 的层缓存规则：某一层的**输入没变**，就直接复用上次结果。
# 所以这里**只拷依赖清单**先装依赖 ——
#   之后改多少 .py 文件都不会触发重装依赖（否则每次 build 都要等 3 分钟）。
COPY pyproject.toml uv.lock ./
# ★ PyPI 加速（2026-10-16 部署实测修订版）：
#   血泪实测：uv.lock 里 1846 处 wheel 下载 URL 是**绝对地址**（files.pythonhosted.org），
#   `uv sync --frozen` 会忠实按这些 URL 下载 —— 只设 UV_DEFAULT_INDEX 没用
#   （实测 12 秒内 100 个请求全打 pythonhosted、零个到清华源；国内直连 pythonhosted
#   仅 ~50-90KB/s，装依赖要 40 分钟+）。
#   解法：先把锁文件里的包 URL 前缀替换为**清华镜像同路径地址**再 sync
#   （两站路径结构一致、文件字节相同，sha256 校验与哈希完全兼容；清华侧 HTTP 206 实测通过）。
#   该改动只作用于这一条 RUN（不进镜像运行时环境）；本机与服务器构建同样受益。
#   如需回退官方源：删掉 sed 前缀与 `UV_DEFAULT_INDEX=... ` 即可。
RUN sed -i 's|https://files.pythonhosted.org/packages/|https://pypi.tuna.tsinghua.edu.cn/packages/|g' uv.lock && \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --frozen

# ---------- 3. 再拷代码 ----------
# 这三份都是运行时真的用到的：
#   app/         → 应用本体
#   scripts/     → 项目约定用 `uv run python -m scripts.xxx` 跑脚本
#   mcp_servers/ → ★ 必须拷：app/mcp/manager.py:181 会起
#                  `python mcp_servers/inventory_server.py` 当本机 MCP server
#   tests/       → ★ D34 验收要求「一键起 + 测试过」，所以镜像必须是
#                  **完整可复现环境**：`docker compose exec api pytest` 要直接能跑。
#                  这也是上面 `uv sync` 不带 `--no-dev` 的同一个理由。
#   metrics.yaml → ★ 语义层（D37）的口径文件，在**项目根**（不在 app/ 里）：
#                  metrics_service 模块级加载它（写错/缺失 = fail fast 启动即炸）。
#                  漏拷时的症状：容器 CrashLoop，日志 FileNotFoundError '/app/metrics.yaml'
#                  —— 2026-10-16 实际踩过（块 1 加的文件、块 2 首次重建镜像才暴露）。
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY mcp_servers/ ./mcp_servers/
COPY tests/ ./tests/
COPY metrics.yaml ./

# ---------- 4. 运行 ----------
EXPOSE 8000

# ★ --host 0.0.0.0 必须写：
#   uvicorn 默认绑 127.0.0.1，那是"**只监听容器自己**"——
#   宿主机的 18000→8000 端口映射会连不上。这和前面讲的
#   "容器里的 localhost 不是你的 mac" 是同一个坑的**另一面**：
#   那边是"客户端地址写错"，这边是"服务端只肯听自己"。
#
# --no-sync：启动时不再校验/同步依赖。镜像里已经装好了，
#   让容器启动时联网同步 = 把启动成功率押在网络上。
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
