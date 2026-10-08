# ============================================================
# AgentForge 前端镜像（D34）
# ============================================================
# **多阶段构建**：一个 Dockerfile 写两段
#   阶段 1 (builder)：有 Node  → 跑 `npm run build`，产出 dist/
#   阶段 2 (运行)    ：换 nginx → 只把 dist/ 搬进来
#
# 所以"含 Node 运行时"指的是**构建链里有 Node**；
# 最终镜像里**没有 Node、没有 node_modules**，只有 nginx + 静态文件。
#
# 类比（前端最熟）：这就是"只把 dist 带进生产，不带 node_modules 和构建工具" ——
# 你平时部署前端本来就这么干，多阶段构建只是把它写进 Dockerfile 而已。
# ============================================================

# ---------- 阶段 1：builder ----------
FROM node:22-alpine AS builder

WORKDIR /app

# npm 源。纯构建期参数（不是环境变量、不进镜像），可用
#   docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org ...
# 覆盖回官方源。默认走国内镜像：本机直连 registry.npmjs.org 不稳定。
ARG NPM_REGISTRY=https://registry.npmmirror.com

# ★ 先只拷"依赖清单"两个文件 —— 这是层缓存的关键技巧：
#   只要 package.json / package-lock.json 没变，下面这层依赖就**直接复用缓存**，
#   你改 100 次 .tsx 都不会重装依赖。
#   若反过来先 COPY . . 再装依赖，那改一行代码就要把 188 个包重装一遍。
COPY package.json package-lock.json ./

# `npm ci`（不是 install）：严格按 package-lock.json 装，可复现、版本不会漂。
RUN npm ci --registry=${NPM_REGISTRY} --no-audit --no-fund

# 再拷源码与构建配置（改代码只会让这一层往后重跑）
COPY tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY vite.config.ts vitest.config.ts index.html ./
COPY src/ ./src/

# 产出 dist/。scripts.build = "tsc -b && vite build"（先类型检查，再打包）
RUN npm run build

# ---------- 阶段 2：运行 ----------
# 本机已缓存 nginx:1.27-alpine（49.7 MB），不用再拉
FROM nginx:1.27-alpine

# 覆盖默认站点配置：静态托管 + /api/ 反代到 api 容器（详见 nginx.conf 注释）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# ★ 只从 builder 搬 dist/ 过来；node_modules（342 MB）一根汗毛都不带
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# 不用写 CMD：nginx 基础镜像自带启动命令
