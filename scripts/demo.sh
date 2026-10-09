#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# AgentForge-Lite · 5 分钟演示脚本（PRD §16.2 / F10.3）
# ═══════════════════════════════════════════════════════════════════════════
# 三条设计原则：
#   ① **能自动化的段就自动跑**（第 2/3 段），不能的打印「操作 + 预期现象」；
#   ② **每段如实标注当前状态**（可演 / 有条件 / 未实现）—— 不假装七段都能演；
#   ③ 断掉时**明确指出断点**，并区分「环境坏了」与「前置没满足」。
#
# 用法：
#   scripts/demo.sh                 完整演示（自动段落自动执行）
#   scripts/demo.sh --dry-run       只做前置检查，不发真实提问
#   scripts/demo.sh --pause         每段后等回车（真人演示时用，控制节奏）
#   scripts/demo.sh --api URL       指定后端（默认 http://localhost:18000）
#   scripts/demo.sh --upload-demo   第 2 段真的演示一次上传动作（会先删同名文档）
#
# 退出码：0 = 演示路径通畅；1 = 有段落失败；2 = 前置不满足（服务没起等）
# ═══════════════════════════════════════════════════════════════════════════

set -uo pipefail

API="http://localhost:18000"
DRY_RUN=0
PAUSE=0
UPLOAD_DEMO=0
FAILED=0
SKIPPED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --api)         API="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --pause)       PAUSE=1; shift ;;
    --upload-demo) UPLOAD_DEMO=1; shift ;;
    -h|--help)     sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

# ── 颜色（非终端自动关闭，便于重定向到日志）──────────────────────────────
if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'
else
  B=""; DIM=""; G=""; Y=""; R=""; C=""; N=""
fi

hr()     { printf '%s\n' "──────────────────────────────────────────────────────────────────────"; }
title()  { printf '\n%s══ %s%s\n' "$B$C" "$1" "$N"; hr; }
seg()    { printf '\n%s▶ 第 %s 段 · %s（%s）%s\n' "$B$C" "$1" "$2" "$3" "$N"; hr; }
ops()    { printf '  %s【操作】%s %s\n' "$B" "$N" "$1"; }
expect() { printf '  %s【预期】%s %s\n' "$B" "$N" "$1"; }
actual() { printf '  %s【实测】%s %s\n' "$B" "$N" "$1"; }
ok()     { printf '  %s✅%s %s\n' "$G" "$N" "$1"; }
warn()   { printf '  %s⚠%s  %s\n' "$Y" "$N" "$1"; }
fail()   { printf '  %s❌%s %s\n' "$R" "$N" "$1"; FAILED=$((FAILED + 1)); }
skip()   { printf '  %s⏭%s  %s\n' "$DIM" "$N" "$1"; SKIPPED=$((SKIPPED + 1)); }
pause_if_needed() {
  if [ "$PAUSE" = "1" ] && [ -t 0 ]; then printf '\n%s（回车继续）%s' "$DIM" "$N"; read -r _; fi
}

TMP=""
cleanup() { [ -n "$TMP" ] && rm -f "$TMP"; }
trap cleanup EXIT
TMP="$(mktemp)"

api_get()  { curl -s --max-time 20 "$API$1"; }
api_post() { curl -s --max-time 180 -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }

# ═══════════════════════════════════════════════════════════════════════════
printf '%s' "$B"
cat <<'BANNER'
  ╔══════════════════════════════════════════════════════════════════╗
  ║   AgentForge-Lite · 5 分钟演示（PRD §16.2 七段）                  ║
  ╚══════════════════════════════════════════════════════════════════╝
BANNER
printf '%s' "$N"
printf '  后端：%s\n' "$API"

title "段落状态总览（先看清楚哪几段现在真能演）"
cat <<'STATUS'
  段  场景                    时长     当前状态
  ─────────────────────────────────────────────────────────────────
  1   开场 · 架构分层         30s     ✅ 可演（讲稿）
  2   知识库 · 上传与 ready   45s     ✅ 可演（脚本自动执行）
  3   RAG 问答 · 答案带引用   1min    ✅ 可演（脚本自动执行）
  4   外部系统互操作          1.5min  ⚠ 有条件：需外部 PAT + 宿主机后端实例
  5   Agentic BI 多跳下钻     1.5min  ❌ 未实现：依赖指标语义层（后续里程碑）
  6   评测 · 消融与对照       1min    ⚠ 部分：本仓已有评测报告；对照报告未做
  7   Langfuse 数据回流       1min    ⚠ 可选段：需追踪凭据与已有数据
  ─────────────────────────────────────────────────────────────────
  ⚠ 第 5 段依赖的「指标语义层」尚未落地（`/api/metrics` 当前不存在），
    脚本会明确标为未实现 —— 演示时如实说明，不要含糊跳过。
STATUS

# ═══════════════════════════════════════════════════════════════════════════
title "前置检查"

HEALTH="$(api_get /api/health)"
if [ -z "$HEALTH" ]; then
  fail "后端不可达：$API —— 请先 `make up`（或 docker compose up -d）"
  echo
  echo "前置不满足，演示无法开始。"
  exit 2
fi
ok "后端可达：$HEALTH"

api_get /api/documents > "$TMP"
DOCS_TOTAL="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$TMP" 2>/dev/null || echo "?")"
DOCS_READY="$(python3 -c 'import json,sys; print(sum(1 for d in json.load(open(sys.argv[1])) if d["status"]=="ready"))' "$TMP" 2>/dev/null || echo "?")"
ok "知识库：共 ${DOCS_TOTAL} 篇，${DOCS_READY} 篇 ready"

api_get /api/tools > "$TMP"
TOOLS_TOTAL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["total"])' "$TMP" 2>/dev/null || echo "?")"
TOOLS_SRC="$(python3 -c 'import json,sys; print("/".join(sorted(json.load(open(sys.argv[1]))["by_source"].keys())))' "$TMP" 2>/dev/null || echo "?")"
ok "工具装载：total=${TOOLS_TOTAL}，来源=${TOOLS_SRC}"

HAS_HARNESS=0
python3 -c 'import json,sys; sys.exit(0 if "harness" in json.load(open(sys.argv[1]))["by_source"] else 1)' "$TMP" && HAS_HARNESS=1
if [ "$HAS_HARNESS" = "1" ]; then
  ok "外部（harness）工具已装载 → 第 4 段可以演"
else
  warn "外部（harness）工具未装载 → 第 4 段将 SKIP（这不是故障，是缺少个人凭据或未跑 make warmup）"
fi

if [ -f docs/eval-report-2026-09-30.md ]; then
  ok "评测报告在仓：docs/eval-report-2026-09-30.md"
else
  warn "未找到评测报告文件"
fi

if [ "$DRY_RUN" = "1" ]; then
  title "「--dry-run」：前置检查完毕，不发起真实提问"
  if [ "$FAILED" -gt 0 ]; then echo "有 ${FAILED} 项异常。"; exit 1; fi
  echo "前置就绪。去掉 --dry-run 即开始完整演示。"
  exit 0
fi

# ═══════════════════════════════════════════════════════════════════════════
seg 1 "开场 · 架构分层" "30s"
ops "打开 docs/架构图.md §8 的 30 秒讲稿，照念；或展示前端首页"
expect "观众在 30 秒内建立心智模型：五层解耦、端到端一条链路"
actual "讲稿在 docs/架构图.md §8「演示第 1 段 · 30 秒讲稿」"
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
seg 2 "知识库 · 上传示例文档 → 状态 ready" "45s"
if [ "$UPLOAD_DEMO" = "1" ]; then
  ops "真的演示一次上传：删除同名文档后重新上传三份示例（scripts/seed_demo_data.py --force）"
  expect "上传接口返回 202 Accepted；稍后状态由 processing 变 ready"
  if uv run python -m scripts.seed_demo_data --force 2>&1 | tail -12; then
    ok "三份示例文档已重新入库"
  else
    fail "播种失败，见上方输出"
  fi
else
  ops "展示知识库现状（要现场演示上传动作请加 --upload-demo）"
  expect "3 份示例文档为 ready，切片数与页码引用正常"
  actual "当前 ${DOCS_READY}/${DOCS_TOTAL} 篇 ready"
  api_get /api/documents > "$TMP"
  python3 - "$TMP" <<'PY'
import json, sys
docs = json.load(open(sys.argv[1]))
for d in docs[:12]:
    print(f"        {d['filename']:<28} {d['status']:<10} 切片={d['chunk_count']}")
print(f"        … 共 {len(docs)} 篇")
PY
fi
ops "（可选）在「知识库」页展开发言：PDF 的引用能定位到具体页码"
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
seg 3 "RAG 问答 · 答案带引用 → 点引用看原文" "1min"
QUESTION="公司员工一年有多少天带薪年休假？"
ops "向 Agent 提问：${QUESTION}"
expect "回答给出 5/10/15 天分档，并附 [n] 编号的来源片段"
api_post /api/chat "{\"message\":\"${QUESTION}\"}" > "$TMP"
python3 - "$TMP" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"        ❌ 响应无法解析：{e}"); raise SystemExit(1)
print("        【答】" + d.get("answer", "")[:500].replace("\n", "\n              "))
srcs = d.get("sources") or []
print(f"        【来源】共 {len(srcs)} 条")
for s in srcs[:5]:
    page = f"  页码={s['page_ref']}" if s.get("page_ref") else ""
    sim = f"  相似度={s['similarity']:.3f}" if s.get("similarity") is not None else ""
    print(f"          [{s['index']}] {s['source']}{page}{sim}")
    print(f"              {s['content'][:80].strip()}…")
PY
if [ $? -ne 0 ]; then fail "问答请求失败"; else ok "问答链路通，答案与来源均已返回"; fi
ops "（可选）在前端「对话台」点引用编号，展示原文定位"
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
seg 4 "外部系统互操作（MCP 工具调用）" "1.5min"
if [ "$HAS_HARNESS" = "0" ]; then
  skip "跳过：外部工具未装载（缺个人凭据 / 未跑 make warmup / 用的是容器内后端）"
  warn "如实说明话术：「外部工具需要个人访问令牌，演示环境未配置，Agent 会自动降级为纯知识库问答」"
  warn "要演这一段：在宿主机跑 uvicorn（8000 端口）并在 .env 配 HARNESS_API_KEY，再跑 make warmup"
else
  ops "提问：我最近哪些流水线失败了，为什么？"
  expect "工具卡片显示「来源：harness」→ Agent 给出失败分类与建议"
  api_post /api/chat '{"message":"我最近哪些流水线失败了，为什么？"}' > "$TMP"
  python3 - "$TMP" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("        【答】" + d.get("answer", "")[:400].replace("\n", "\n              "))
print(f"        【本轮调用的工具】{d.get('tool_calls') or '（无）'}")
PY
  ok "外部工具段可演示"
fi
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
seg 5 "Agentic BI 分析（多跳下钻）" "1.5min"
skip "未实现：依赖「指标语义层」（指标名 / 维度 / 时间参数白名单），属于后续里程碑"
warn "当前没有 /api/metrics 接口，Agent 无法从语义层选指标 —— 这一段**现在演不了**"
warn "演示时如实说明：「这一段是路线图上的能力，当前版本的多跳分析尚未落地」"
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
seg 6 "评测 · 消融与对比报告" "1min"
ops "打开 docs/eval-report-2026-09-30.md（或前端「评测看板」页）"
expect "展示三配置（纯向量 / 混合 / 混合+重排）的准确率对比与失败案例"
if [ -f docs/eval-report-2026-09-30.md ]; then
  actual "报告在仓：docs/eval-report-2026-09-30.md（$(wc -l < docs/eval-report-2026-09-30.md | tr -d ' ') 行）"
  ok "评测段可演（用已生成报告，避免现场等待）"
else
  fail "评测报告缺失"
fi
warn "对照报告（与外部平台的同题对照）尚未产出 → 这一段只演本项目的消融对比"
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
seg 7 "（可选）Langfuse 数据回流" "1min"
skip "可选加段：需追踪凭据 + 已有追踪数据；限时 5 分钟时本段本就该砍"
warn "PRD §16.2 明确：第 7 段是「应该」级指标，不是必须项"
pause_if_needed

# ═══════════════════════════════════════════════════════════════════════════
title "演示结束 · 收尾提示"
cat <<'TAIL'
  时长对照（PRD §16.2）：
    · 五段核心（1–3 + 6 + 有话可说）= 约 5 分钟
    · 加上第 4、5 段 ≈ 6.5 分钟（本版本第 5 段尚不可演）
    · 再加第 7 段 ≈ 7.5 分钟

  限时 5 分钟的降级顺序（照 PRD 建议）：
    ① 第 6 段的对照报告改为一句带过（"报告在文档里"）
    ② 第 7 段直接砍掉（可选段）
    ③ 第 5 段的三跳下钻只演两跳（本版本整段暂不可演）

  演示后清理（让知识库回到只有评测语料的状态）：
    make seed-clean
TAIL

printf '\n'
if [ "$FAILED" -gt 0 ]; then
  printf '%s有 %s 项失败，请先处理再演示。%s\n' "$R" "$FAILED" "$N"
  exit 1
fi
printf '%s✅ 演示路径通畅%s（跳过 %s 段，原因见上）\n' "$G" "$N" "$SKIPPED"
exit 0
