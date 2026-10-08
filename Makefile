# AgentForge · 快捷入口
# ======================
# 存在的理由：把几条"记不住就容易漏"的动作，变成不会写错的命令。
#   - `make warmup` = PRD §17.3 演示前置检查清单第 2 项，也是 §9.8「或提供 make warmup」的兑现
#
# ⚠ 两条本项目特有的规矩，写在这里免得每次都要回去翻：
#   ① python 侧一律 `uv run python -m scripts.xxx` —— **`-m` 才把项目根塞进 sys.path**；
#      直接给路径会 `ModuleNotFoundError: No module named 'app'`（长得像依赖坏了，其实是入口写法错）。
#   ② 全部回归脚本的输出**落盘**（logs/）—— 用 `| tail -N` 抓汇总会把汇总行吃掉。

SHELL := /bin/bash

# uv 可能不在非交互 shell 的 PATH 里 → 取不到就退回默认安装位置
UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

LOG_DIR := logs

# 全量回归的脚本清单（不含 harness 依赖项 —— 那两个要 `--with-harness`，别混进日常回归）
VERIFY_SCRIPTS := d21_delete_scope d21_eval d22 d23_metrics d24_report d26 d27 d28

.PHONY: help up down restart logs ps warmup warmup-dry test verify web-dev web-build

help:
	@echo "AgentForge 常用命令"
	@echo ""
	@echo "  部署"
	@echo "    make up          起全部容器（db/redis/api/web）并构建镜像"
	@echo "    make down        停掉容器（保留数据卷）"
	@echo "    make restart     重建并重启"
	@echo "    make logs        跟踪全部容器日志"
	@echo "    make ps          看容器状态"
	@echo ""
	@echo "  演示前"
	@echo "    make warmup      ★ 预热 MCP（演示前必跑；日志落 $(LOG_DIR)/）"
	@echo "    make warmup-dry  只查缓存与配置，不起进程（秒级）"
	@echo ""
	@echo "  测试"
	@echo "    make test        跑 pytest（tests/）"
	@echo "    make verify      跑全量回归脚本（日志落 $(LOG_DIR)/）"
	@echo ""
	@echo "  前端"
	@echo "    make web-dev     前端本地开发（vite，代理到 :8000）"
	@echo "    make web-build   前端生产构建"

# ------------------------------------------------------------------ 部署
up:
	docker compose up -d --build
	@docker compose ps

down:
	docker compose down

restart:
	docker compose down
	docker compose up -d --build
	@docker compose ps

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

# ------------------------------------------------------------------ 演示前
warmup:
	@mkdir -p $(LOG_DIR)
	@set -o pipefail; $(UV) run python -m scripts.warmup_mcp 2>&1 | tee $(LOG_DIR)/warmup-$$(date +%Y%m%d-%H%M%S).log

warmup-dry:
	$(UV) run python -m scripts.warmup_mcp --dry-run

# ------------------------------------------------------------------ 测试
test:
	$(UV) run pytest -q

verify:
	@mkdir -p $(LOG_DIR)
	@for s in $(VERIFY_SCRIPTS); do \
	  printf '\n=== %s ===\n' "$$s"; \
	  if $(UV) run python -m scripts.$$s > $(LOG_DIR)/verify-$$s.log 2>&1; then \
	    echo "  ✅ 通过"; \
	  else \
	    echo "  ★ 失败 —— 见 $(LOG_DIR)/verify-$$s.log"; \
	  fi; \
	done
	@echo ""
	@echo "各脚本的断言汇总（每行来自对应日志）："
	@for s in $(VERIFY_SCRIPTS); do \
	  printf '  %-20s ' "$$s"; \
	  grep -hE "PASS=[0-9]+" $(LOG_DIR)/verify-$$s.log | tail -1 || echo "（日志里找不到 PASS= 汇总行）"; \
	done

# ------------------------------------------------------------------ 前端
web-dev:
	cd frontend && npm run dev

web-build:
	cd frontend && npm run build
