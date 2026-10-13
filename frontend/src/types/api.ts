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
