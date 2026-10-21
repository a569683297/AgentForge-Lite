export interface HealthResponse {
  status: 'ok' | 'degraded';
  version: string;
  deps: { api: string; redis: string };
}

export interface SourceItem {
  index: number;
  source: string;
  content: string;
  similarity: number | null;
  page_ref: string | null;
}

/**
 * 本轮回答的**实际执行模型**（S2 归因数据，`model_used` 字段的形状）。
 *
 * 三字段各回答一个问题（与后端 `app/schemas/chat.py::ModelUsed` 对齐）：
 *   id    —— 配置里的哪个（内置通道名如 "deepseek" / 自定义模型 UUID，与请求 model_id 同口径）
 *   name  —— 界面怎么称呼它（"DeepSeek" / 用户起的名字）
 *   model —— 发给端点的真实模型标识（如 "deepseek-chat"）
 *
 * 注意：它是"**实际**用了什么"，不是"用户选了什么"——降级等场景下两者可能不同。
 */
export interface ModelUsed {
  id: string;
  name: string;
  model: string;
}

export interface ChatResponse {
  session_id: string;
  answer: string;
  sources: SourceItem[];
  invalid_citations: number[];
  tool_calls: string[];
  model_used: ModelUsed | null;
  /** 本轮 query_metric 的结构化结果（D37 块 3；非流式出口与 done 帧同名字段对齐） */
  metric_results: MetricResultItem[];
}

/**
 * 模型列表项（`GET /api/models`；S1 模型注册表，与后端 `LlmModelOut` 对齐）。
 *
 * `kind` 三值：builtin=内置（.env）· local=自定义但指向本机 · custom=其余自定义。
 * 内置模型 id 是通道名（如 "deepseek"）；自定义模型 id 是 UUID。
 */
export interface ModelItem {
  id: string;
  name: string;
  model: string;
  kind: 'builtin' | 'local' | 'custom';
  enabled: boolean;
  base_url: string | null;
  api_key_masked: string | null;
}

/**
 * 连通性测试结果（`POST /api/models/test` 与 `/api/models/{id}/test`）。
 * 失败也是 **200**（ok=false + error 人话）——"通不通"正是接口要回答的问题本身。
 */
export interface ModelTestResult {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
  sample: string | null;
}

/** 连通性测试入参（对**表单里还没保存**的配置测试） */
export interface ModelTestInput {
  base_url: string;
  api_key?: string;
  model: string;
}

/** 新建/编辑自定义模型的表单载荷（编辑时字段可选；api_key 留空 = 不修改） */
export interface ModelInput {
  name?: string;
  base_url?: string;
  api_key?: string;
  model?: string;
  enabled?: boolean;
}

/**
 * `POST /api/chat/stream` 的 SSE 事件（帧协议的唯一前端出处）。
 *
 * 与后端对齐位置：`app/services/agent_service.py::run_agent_stream` 的 docstring
 * （服务层定义事件形状）+ `app/api/chat.py::chat_stream` 的 docstring（接口层定义编帧）。
 * 改协议时三处一起改。
 */
export type ChatStreamEvent =
  | { type: 'meta'; session_id: string }
  | { type: 'token'; text: string }
  | { type: 'reset' }
  | { type: 'tool'; name: string; phase: 'start' | 'end'; ok?: boolean }
  | {
      type: 'done';
      session_id: string;
      answer: string;
      sources: SourceItem[];
      invalid_citations: number[];
      tool_calls: string[];
      model_used: ModelUsed | null;
      /** 本轮 query_metric 的结构化结果（D37 块 3）——与 sources 同族的"结构给前端"通道 */
      metric_results: MetricResultItem[];
    }
  | { type: 'error'; message: string };

export interface DocumentOut {
  id: string;
  filename: string;
  file_type: string;
  status: 'processing' | 'ready' | 'failed';
  chunk_count: number;
  error_message: string | null;
  created_at: string;
}

export interface DocumentChunkPreview {
  chunk_index: number;
  content: string;
  page_ref: string | null;
}

export interface DocumentPreviewOut {
  document: DocumentOut;
  chunks: DocumentChunkPreview[];
}

export interface SessionItem {
  id: string;
  title: string;
  updated_at: string;
}

export interface ToolCallPayload {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

export interface MessageItem {
  role: 'user' | 'assistant' | 'tool';
  content: string;
  tool_calls: ToolCallPayload[] | null;
  created_at: string;
}

export interface EvalCaseOut {
  id: number;
  case_key: string;
  category: 'doc_qa' | 'cross_doc' | 'tool_call';
  difficulty: 'easy' | 'medium' | 'hard';
  is_negative: boolean;
  question: string;
}

export interface EvalCaseDetailOut extends EvalCaseOut {
  reference: string;
  evidence: string;
  doc_slugs: string[];
  expected_tool: string | null;
}

export interface EvalDatasetOut {
  total: number;
  negative: number;
  by_category: Record<string, number>;
  by_difficulty: Record<string, number>;
  covered_slugs: string[];
  fingerprint: string;
}

export interface EvalRunOut {
  id: number;
  config_name: string;
  dataset_fingerprint: string | null;
  corpus_fingerprint: string | null;
  judge_model: string | null;
  generation_runs: number | null;
  runs_per_case: number | null;
  score_correctness: number | null;
  score_faithfulness: number | null;
  score_completeness: number | null;
  accuracy: number | null;
  report_path: string | null;
  created_at: string;
}

export interface EvalCategoryStatOut {
  category: string;
  rows: number;
  cases: number;
  passed_rows: number;
  avg_correctness: number | null;
  avg_faithfulness: number | null;
  avg_completeness: number | null;
}

export interface EvalRunDetailOut extends EvalRunOut {
  by_category: EvalCategoryStatOut[];
  failure_breakdown: Record<string, number>;
  rows: number;
  scored_rows: number;
  generation_inconsistent: number;
}

export interface EvalCaseResultOut {
  id: number;
  case_key: string;
  category: string;
  generation_index: number;
  answer: string;
  tool_calls: string[] | null;
  sources_count: number;
  score_correctness: number | null;
  score_faithfulness: number | null;
  score_completeness: number | null;
  judge_runs: number;
  passed: boolean;
  failure_reason: string | null;
  failure_reason_label: string;
}

export interface EvalCaseResultDetailOut extends EvalCaseResultOut {
  retrieved_chunks: string;
  judge_raw: unknown[] | null;
}

export interface ToolItem {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  source: string;
}

export interface MCPServerStatus {
  name: string;
  connected: boolean;
  tools: string[];
  tool_count?: number;
  server_info?: { name: string | null; version: string | null } | null;
  protocol_version?: string;
  stderr_log: string | null;
  error?: string | null;
}

export interface ToolsResponse {
  total: number;
  by_source: Record<string, string[]>;
  servers: MCPServerStatus[];
  tools: ToolItem[];
}

// ────────────────────────────────────────────────────────────────
// 用户上传的工具 server（与后端 app/schemas/tool_server.py 对齐）
//
// 两个状态字段**故意不合并**：
//   · status    —— 库里的事实：它「该不该跑」（active / disabled / failed）
//   · connected —— 运行态：它「此刻在不在跑」
// active 但 disconnected 是有意义的异常：容器崩了、开机没起来。
// 折叠成一个字段就看不见这种"该跑却没跑"的情况了。
// ────────────────────────────────────────────────────────────────
export type ToolServerStatus = 'active' | 'disabled' | 'failed';

export interface ToolServerOut {
  id: string;
  name: string;
  display_name: string;
  description: string;
  status: ToolServerStatus;
  connected: boolean;
  tools: string[];
  tool_count: number;
  last_error: string | null;
  size_bytes: number;
  sha256: string;
  created_at: string;
  updated_at: string;
}

export interface ToolServerListResponse {
  total: number;
  servers: ToolServerOut[];
}

// ────────────────────────────────────────────────────────────────
// 语义层（D37 / PRD §9.10 · F11）
// 与后端 app/schemas/metrics.py 对齐。三类类型的分工：
//   · MetricInfo       —— GET /api/metrics 的目录项（口径说明的只读出口）
//   · MetricsQueryOut  —— POST /api/metrics/query 的响应（HTTP 直接消费）
//   · MetricResultItem —— done 帧 / ChatResponse 里的结构化结果（比 HTTP 版多
//                         中文名与数据窗口——那是"对话里查了什么"的归因需要）
// ────────────────────────────────────────────────────────────────

/**
 * 指标数据源二值：`sql` = 项目 PostgreSQL；`langfuse_api` = Langfuse 运行追踪。
 *
 * 用字面量联合而不是 string 是刻意的：**只有 langfuse 源会降级**（degraded），
 * 消费方（如分析区的 Token 卡）按 source 分支渲染的特性交给类型系统把关 —— 
 * 若后端将来加入第三种源，这里会直接编译报错，而不是静默走进某个分支。
 */
export type MetricSource = 'sql' | 'langfuse_api';

/** `GET /api/metrics` 列表项：一个指标的"口径目录"信息（与后端 MetricInfo 对齐）。 */
export interface MetricInfo {
  name: string;
  label: string;
  description: string;
  source: MetricSource;
  /** 可分组维度白名单（空数组 = 聚合指标，不支持分组） */
  dimensions: string[];
}

/**
 * 指标结果的一行：形状由该指标定义（列名随指标而变），所以按字典透传不强约束。
 * 约定：每行至少含 `value`（主数值列）；分组查询多一列 `dim`；
 * langfuse 源的分位数指标还带 `count`（样本数）。
 */
export type MetricRow = Record<string, unknown>;

/** `POST /api/metrics/query` 的响应（与后端 MetricsQueryOut 对齐）。 */
export interface MetricsQueryOut {
  metric: string;
  dimension: string | null;
  source: MetricSource;
  rows: MetricRow[];
  /**
   * langfuse 源专有的降级标记：数据源不可达时 true（rows 为空、error 给原因）。
   * 注意此时 HTTP 仍是 200 ——"该指标暂不可用"是正常业务状态，渲染占位而不是报错。
   * sql 源恒为 false。
   */
  degraded: boolean;
  error: string | null;
}

/**
 * `POST /api/metrics/overview` 的响应（与后端 MetricsOverviewOut 对齐）。
 *
 * 为什么按**指标名**索引而不是数组：数组会让每个消费方自己 `find(r => r.metric === x)`，
 * 写错名字就是静默 undefined；字典取不到同样是 undefined，但调用处短得多、
 * 也更容易在 code review 里看出来。metric 名唯一性由后端保证（重复会 400）。
 */
export interface MetricsOverviewOut {
  start_date: string;
  end_date: string;
  results: Record<string, MetricsQueryOut>;
}

/** done 帧 / ChatResponse 里的结构化查询结果（与后端 MetricResultItem 对齐）。 */
export interface MetricResultItem extends MetricsQueryOut {
  /** 指标中文名（画图标题用，不必再查目录接口） */
  label: string;
  /** 数据窗口起始日 YYYY-MM-DD（北京时间，含当天） */
  start_date: string;
  /** 数据窗口结束日 YYYY-MM-DD（含当天） */
  end_date: string;
}

// ────────────────────────────────────────────────────────────────
// Trace（真实观测数据，来自 Langfuse v2 observations）
// ────────────────────────────────────────────────────────────────
/**
 * 树里的一个节点。
 *
 * `start_ms` / `end_ms` 是**相对本次 trace 起点**的毫秒，不是时间戳 ——
 * 瀑布图的横轴只关心"谁先开始、持续多久"，绝对时间在 `start_time` 里。
 * 顺序即渲染顺序（父一定在子之前），前端不需要递归，按数组画、用 depth 缩进即可。
 */
export interface TraceSpanItem {
  id: string;
  parent_id: string | null;
  name: string;
  type: string;
  depth: number;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  /**
   * `warning` 不是失败：典型场景是重排超时降级（退回融合序）。
   * 页面用黄色区分于红色的真错误 —— 两者的处置方式完全不同。
   */
  status: 'ok' | 'warning' | 'error';
  status_message: string | null;
  model: string | null;
}

/**
 * 列表页一行 —— 就是「一次提问」的抬头（不建树，只给汇总）。
 *
 * `kind` 是根 span 名映射出来的类型：dialog（真实对话）/ inspect（检索检查器）/
 * eval（评测判分）/ other。同一项目里三类运行混在一起，不分类的话
 * 对话会被调试运行从列表里挤出去。
 *
 * `question` 来自 messages.trace_id 反查；null = 该次早于关联记录（历史对话）
 * 或评测运行（persist=false 不落库）—— 页面对这两种情况显示「未记录提问」，
 * 不要用 `name` 去顶替，那会让人以为看到了提问原文。
 */
export interface TraceSummaryItem {
  trace_id: string;
  name: string;
  kind: TraceKind;
  question: string | null;
  session_id: string | null;
  start_time: string;
  duration_ms: number;
  span_count: number;
  has_error: boolean;
}

/** 根 span 名映射出的类型（与后端 trace_service.KINDS 一一对应）。 */
export type TraceKind = 'dialog' | 'inspect' | 'eval' | 'other';

/** `GET /api/traces` 的响应。degraded=true 时 HTTP 仍是 200，traces 为空。 */
export interface TraceListResponse {
  degraded: boolean;
  reason: string | null;
  window_hours: number;
  fetched_at: string;
  /** 窗口内 span 超过单次上限，只统计了最近一批（耗时数字会偏小，不是错误） */
  truncated: boolean;
  /** 各类型条数，**按 kind 过滤前**统计（筛选器上显示「仅对话 3」靠它） */
  kind_counts: Partial<Record<TraceKind, number>>;
  traces: TraceSummaryItem[];
}

/** `GET /api/traces/{id}` 的响应。 */
export interface TraceDetailResponse {
  degraded: boolean;
  reason: string | null;
  trace_id: string;
  name: string;
  kind: TraceKind;
  question: string | null;
  session_id: string | null;
  start_time: string;
  /** 整条 trace 的墙钟跨度，瀑布图横轴上限 */
  total_ms: number;
  span_count: number;
  spans: TraceSpanItem[];
}

// ────────────────────────────────────────────────────────────────
// 检索检查器（真实数据，来自 /api/retriever/search）
// ────────────────────────────────────────────────────────────────
/** 页面初始化选项，由后端给（避免前端硬编码配置名与 k 上限）。 */
export interface RetrieverOptions {
  configs: string[];
  default_config: string;
  max_top_k: number;
  default_top_k: number;
}

/**
 * 候选池里的一条片段。
 *
 * 三个名次必须分开看，混用会让"取舍"这件事看不出来：
 *   · `pool_rank`   候选池名次（融合序）—— 左列的行序
 *   · `rerank_rank` 重排后的完整名次 —— "被挤到第 12 名"
 *   · `final_rank`  最终 top-k 名次 —— 只有前 k 条有值，null = 被淘汰
 *
 * 四种分数量纲不可比：similarity ∈ [0,1]、bm25_score 无上界、
 * rrf_score 是 1/(60+名次) 累加（0.016~0.033）、rerank_score 是 logit。
 */
export interface RetrieverCandidate {
  pool_rank: number;
  chunk_id: string;
  doc_id: string | null;
  source: string | null;
  page_ref: string | null;
  content: string;
  /** 候选池阶段的主分数：similarity（纯向量）或 rrf_score（融合） */
  score: number | null;
  similarity: number | null;
  bm25_score: number | null;
  rrf_score: number | null;
  vector_rank: number | null;
  bm25_rank: number | null;
  rerank_rank: number | null;
  final_rank: number | null;
  /** 重排 logit；只有真跑过重排才有，降级与非重排配置为 null */
  rerank_score: number | null;
  /** null = 本次查询没有 gold（未选题 / 该题无依据文档） */
  is_gold: boolean | null;
}

/** 一道评测题的判定结果（文档级去重口径）。 */
export interface RetrieverGold {
  case_key: string;
  question: string;
  category: string;
  is_negative: boolean;
  doc_slugs: string[];
  has_gold: boolean;
  expected_count: number;
  hit_count: number;
  hit: boolean;
  hit_rank: number | null;
  mrr: number;
}

export interface RetrieverTimings {
  retrieve_ms: number;
  rerank_ms: number;
  resolve_ms: number;
  total_ms: number;
}

export interface RetrieverSearchResponse {
  query: string;
  requested_config: string;
  /** 实际走通的路径：重排降级时是 hybrid，不是 hybrid_rerank */
  effective_config: string;
  reranked: boolean;
  degrade_reason: string | null;
  score_kind: 'similarity' | 'rrf_score' | 'rerank_logit';
  top_k: number;
  candidate_k: number;
  timings: RetrieverTimings;
  candidates: RetrieverCandidate[];
  gold: RetrieverGold | null;
}

export interface RetrieverSearchRequest {
  query: string;
  top_k: number;
  config?: string;
  case_key?: string | null;
}
