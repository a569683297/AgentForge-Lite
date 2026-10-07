import type { MetricPoint } from '../components/MetricChart';

export interface MockSpan {
  id: string;
  name: string;
  type: 'agent' | 'retrieval' | 'embedding' | 'rerank' | 'tool' | 'llm';
  start_ms: number;
  end_ms: number;
  depth: number;
  status: 'ok' | 'error';
  input: string;
  output: string;
  tokens: number;
}

export interface MockTrace {
  trace_id: string;
  name: string;
  created_at: string;
  total_ms: number;
  spans: MockSpan[];
}

/** 等后端暴露 trace 查询接口（当前 Langfuse 数据无 REST 出口）。 */
export const MOCK_TRACES: MockTrace[] = [{
  trace_id: 'tr_20261007_8f31',
  name: '为什么混合检索优于纯向量？',
  created_at: '2026-10-07T10:24:16+08:00',
  total_ms: 842,
  spans: [
    { id: 'sp-01', name: 'agent-plan', type: 'agent', start_ms: 0, end_ms: 54, depth: 0, status: 'ok', input: '用户问题', output: '需要查询知识库', tokens: 382 },
    { id: 'sp-02', name: 'search_documents', type: 'tool', start_ms: 62, end_ms: 138, depth: 1, status: 'ok', input: '{"query":"混合检索"}', output: '命中 8 个片段', tokens: 0 },
    { id: 'sp-03', name: 'embedding', type: 'embedding', start_ms: 67, end_ms: 111, depth: 2, status: 'ok', input: '查询文本', output: '向量维度 768', tokens: 0 },
    { id: 'sp-04', name: 'vector-search', type: 'retrieval', start_ms: 114, end_ms: 182, depth: 2, status: 'ok', input: 'top_k=20', output: '候选 20 条', tokens: 0 },
    { id: 'sp-05', name: 'bm25-search', type: 'retrieval', start_ms: 118, end_ms: 205, depth: 2, status: 'ok', input: 'query terms=2', output: '候选 20 条', tokens: 0 },
    { id: 'sp-06', name: 'rrf-fusion', type: 'retrieval', start_ms: 212, end_ms: 238, depth: 2, status: 'ok', input: 'k=60', output: '融合 26 条', tokens: 0 },
    { id: 'sp-07', name: 'bge-reranker', type: 'rerank', start_ms: 246, end_ms: 386, depth: 2, status: 'ok', input: '候选 20 条', output: '重排 top-5', tokens: 0 },
    { id: 'sp-08', name: 'agent-answer', type: 'llm', start_ms: 402, end_ms: 690, depth: 1, status: 'ok', input: '工具结果 + 历史上下文', output: '生成带引用回答', tokens: 1_284 },
    { id: 'sp-09', name: 'citation-check', type: 'agent', start_ms: 698, end_ms: 731, depth: 1, status: 'ok', input: '回答中的 [n]', output: '引用 3 条，合法', tokens: 0 },
    { id: 'sp-10', name: 'trace-flush', type: 'agent', start_ms: 738, end_ms: 754, depth: 1, status: 'ok', input: 'observation spans', output: '已写入 Langfuse', tokens: 0 },
    { id: 'sp-11', name: 'summary-write', type: 'agent', start_ms: 760, end_ms: 778, depth: 1, status: 'ok', input: '会话摘要', output: '已写入 PG', tokens: 0 },
    { id: 'sp-12', name: 'response-serialize', type: 'agent', start_ms: 782, end_ms: 801, depth: 1, status: 'ok', input: 'ChatResponse', output: 'JSON', tokens: 0 },
    { id: 'sp-13', name: 'http-send', type: 'agent', start_ms: 805, end_ms: 825, depth: 1, status: 'ok', input: 'JSON body', output: '200 OK', tokens: 0 },
    { id: 'sp-14', name: 'client-render', type: 'agent', start_ms: 828, end_ms: 842, depth: 1, status: 'ok', input: 'response', output: '消息列表更新', tokens: 0 },
  ],
}];

export interface MockRetrievalRow {
  rank_before: number;
  rank_after: number | null;
  doc: string;
  score_logit: number;
  is_gold: boolean;
}

/** 等后端返回结构化检索片段（当前只有 retrieved_chunks 一段字符串）。 */
export const MOCK_RETRIEVAL: MockRetrievalRow[] = Array.from({ length: 20 }, (_, index) => {
  const order = [3, 1, 8, 2, 5];
  const rankAfter = order.indexOf(index + 1);
  return {
    rank_before: index + 1,
    rank_after: rankAfter >= 0 ? rankAfter + 1 : null,
    doc: ['D17-RRF混合检索.md', 'D19-重排落地.md', 'D13-BM25与tsvector.md', 'PRD-v4.1.md'][index % 4],
    score_logit: Number((1.92 - index * 0.071).toFixed(3)),
    is_gold: index === 1 || index === 7,
  };
});

/** 等 D35 的 GET /api/metrics（语义层）。 */
export const MOCK_METRICS: Array<{ metric: string; dimension: string; points: MetricPoint[] }> = [{
  metric: 'agent_latency',
  dimension: 'daily',
  points: [
    { date: '10/01', value: 810 }, { date: '10/02', value: 772 }, { date: '10/03', value: 835 },
    { date: '10/04', value: 728 }, { date: '10/05', value: 690 }, { date: '10/06', value: 716 },
    { date: '10/07', value: 642 },
  ],
}];

/** 等后端 GET /api/metrics：分析页汇总卡与热门工具。 */
export const MOCK_ANALYTICS_SUMMARY = {
  totalRequests: 1284,
  averageLatencyMs: 642,
  tokenUsage: '1.82M',
  successRate: '98.4%',
  popularTools: [
    { name: 'search_documents', count: 72 },
    { name: 'harness__list_pipelines', count: 46 },
    { name: 'current_time', count: 24 },
    { name: 'inventory_query_inventory', count: 18 },
  ],
};
