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

export interface ChatResponse {
  session_id: string;
  answer: string;
  sources: SourceItem[];
  invalid_citations: number[];
  tool_calls: string[];
}

export interface DocumentOut {
  id: string;
  filename: string;
  file_type: string;
  status: 'processing' | 'ready' | 'failed';
  chunk_count: number;
  error_message: string | null;
  created_at: string;
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
