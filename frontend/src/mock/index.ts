import type { MetricPoint } from '../components/MetricChart';

/**
 * 注意：本文件是**全站唯一的 mock 出口**（`IMPL-SPEC.md` §6.3 的要求：无数据源的页面集中 mock，不得虚构接口）。
 *
 * 本文件里的三块数据分别等三个**尚不存在**的后端出口：
 *   · `MOCK_TRACES`            —— Trace 页：**后端完全没有 trace 查询接口**（span 在 Langfuse，无 REST 出口）
 *   · `MOCK_RETRIEVAL`         —— 检索检查器：现在只有 `retrieved_chunks: str`（**一段字符串**，无名次 / 无 logit / 无 gold）
 *   · `MOCK_METRICS` / `MOCK_ANALYTICS_SUMMARY` —— 分析页：`GET /api/metrics` 属 **D35 语义层**
 *
 * 规矩：mock **可以**是编的数字，但**命名与形状必须照抄真实口径** —— 否则接真数据时形状对不上，
 * 而且演示时会说出错的名字（比"数字是假的"更容易穿帮）。
 */

export interface MockSpan {
  id: string;
  name: string;
  /** 取自 D28 真实埋点里的 `as_type`（见 `app/services/observability.py`）。 */
  type: 'agent' | 'generation' | 'tool' | 'retriever' | 'embedding';
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

/**
 * Trace 页示例数据 —— **span 名与父子形状照抄 D28 的真实埋点**。
 *
 * 真实口径 = `app/services/observability.py` 的 6 个 `SPAN_*` 常量 + `tool_span_name()`：
 *
 *     run_agent                 ← 根，一整轮对话（as_type=agent，带 session_id）
 *     ├─ agent-plan             ← generation（llm_gateway 建的）
 *     ├─ tool:search_documents  ← tool，metadata.source = "local"
 *     │   └─ retrieval          ← retriever 父（**嵌套计时**：它的耗时包含子段）
 *     │       ├─ vector_search  ← retriever
 *     │       │   └─ embedding  ← embedding（只有它能带 model 名）
 *     │       ├─ bm25           ← retriever
 *     │       └─ rerank         ← retriever
 *     ├─ tool:harness_list / tool:harness_get / tool:current_time
 *     └─ agent-answer           ← generation
 *
 * 四处最容易写错的地方（原示例数据就写错了四处）：
 *   ① 向量那格叫 `vector_search`（下划线），**不是** `vector-search`；
 *   ② BM25 那格就叫 `bm25`，**不是** `bm25-search`；
 *   ③ 重排那格叫 `rerank`，**不是** `bge-reranker`（模型名 ≠ span 名）；
 *   ④ 工具那格带 **`tool:` 前缀**（`tool_span_name()` 加的），**不是**光秃秃的工具名。
 */
export const MOCK_TRACES: MockTrace[] = [{
  trace_id: 'tr_20261007_8f31',
  name: '为什么混合检索优于纯向量？',
  created_at: '2026-10-07T10:24:16+08:00',
  total_ms: 842,
  spans: [
    { id: 'sp-01', name: 'run_agent', type: 'agent', start_ms: 0, end_ms: 842, depth: 0, status: 'ok', input: '为什么混合检索优于纯向量？', output: '带引用的回答（3 条来源）', tokens: 0 },
    { id: 'sp-02', name: 'agent-plan', type: 'generation', start_ms: 0, end_ms: 54, depth: 1, status: 'ok', input: '用户问题 + 系统提示', output: '决定调用 search_documents', tokens: 382 },
    { id: 'sp-03', name: 'tool:search_documents', type: 'tool', start_ms: 62, end_ms: 470, depth: 1, status: 'ok', input: '{"query":"混合检索优于纯向量"}', output: '命中 20 个片段 → 重排后 5 条', tokens: 0 },
    { id: 'sp-04', name: 'retrieval', type: 'retriever', start_ms: 66, end_ms: 462, depth: 2, status: 'ok', input: '{"query":"混合检索优于纯向量","config":"hybrid_rerank","top_k":5}', output: '实走 vector_search + bm25 + rerank', tokens: 0 },
    { id: 'sp-05', name: 'vector_search', type: 'retriever', start_ms: 70, end_ms: 186, depth: 3, status: 'ok', input: '{"query":"混合检索优于纯向量","top_k":20}', output: '候选 20 条', tokens: 0 },
    { id: 'sp-06', name: 'embedding', type: 'embedding', start_ms: 72, end_ms: 112, depth: 4, status: 'ok', input: '查询文本', output: '向量维度 768（model=bge-m3）', tokens: 0 },
    { id: 'sp-07', name: 'bm25', type: 'retriever', start_ms: 74, end_ms: 205, depth: 3, status: 'ok', input: '{"query":"混合检索优于纯向量","top_k":20}', output: '候选 20 条（tsvector + 自算 BM25）', tokens: 0 },
    { id: 'sp-08', name: 'rerank', type: 'retriever', start_ms: 214, end_ms: 448, depth: 3, status: 'ok', input: '{"candidates":26,"top_n":5}', output: '精排后 top-5', tokens: 0 },
    { id: 'sp-09', name: 'tool:harness_list', type: 'tool', start_ms: 482, end_ms: 528, depth: 1, status: 'ok', input: '{"account":"…"}', output: '返回 6 条 pipeline', tokens: 0 },
    { id: 'sp-10', name: 'tool:harness_get', type: 'tool', start_ms: 534, end_ms: 596, depth: 1, status: 'ok', input: '{"pipeline_id":"…"}', output: '返回 pipeline 详情', tokens: 0 },
    { id: 'sp-11', name: 'tool:current_time', type: 'tool', start_ms: 602, end_ms: 610, depth: 1, status: 'ok', input: '{}', output: '2026-10-07T10:24:16+08:00', tokens: 0 },
    { id: 'sp-12', name: 'agent-answer', type: 'generation', start_ms: 622, end_ms: 842, depth: 1, status: 'ok', input: '工具结果 + 检索片段 + 历史上下文', output: '生成带 [1][2][3] 引用的回答', tokens: 1_284 },
  ],
}];

export interface MockRetrievalRow {
  rank_before: number;
  rank_after: number | null;
  doc: string;
  score_logit: number;
  is_gold: boolean;
}

/**
 * 检索检查器示例数据 —— 等后端返回**结构化**检索片段
 * （现在 `EvalCaseResultDetailOut.retrieved_chunks` 只有一段字符串，没有名次 / logit / gold 命中）。
 *
 * 注意：`doc` 用的是 `docs/tutorials/` 里**真实存在**的教程文件名（2026-10-07 核对过），
 * 换掉原示例里三个不存在的文件名 —— 这一页是把片段名摆给用户看的，名字编造等于误导。
 */
export const MOCK_RETRIEVAL: MockRetrievalRow[] = Array.from({ length: 20 }, (_, index) => {
  const order = [3, 1, 8, 2, 5];
  const rankAfter = order.indexOf(index + 1);
  return {
    rank_before: index + 1,
    rank_after: rankAfter >= 0 ? rankAfter + 1 : null,
    doc: [
      'D17-RRF混合检索.md',
      'D19-重排落地与检索配置化.md',
      'D13-BM25与tsvector原理.md',
      'D18-重排原理与bge-reranker.md',
    ][index % 4],
    score_logit: Number((1.92 - index * 0.071).toFixed(3)),
    is_gold: index === 1 || index === 7,
  };
});

/** 等后端 `GET /api/metrics`（属 **D35 语义层**）：分析页的曲线。 */
export const MOCK_METRICS: Array<{ metric: string; dimension: string; points: MetricPoint[] }> = [{
  metric: 'agent_latency',
  dimension: 'daily',
  points: [
    { date: '10/01', value: 810 }, { date: '10/02', value: 772 }, { date: '10/03', value: 835 },
    { date: '10/04', value: 728 }, { date: '10/05', value: 690 }, { date: '10/06', value: 716 },
    { date: '10/07', value: 642 },
  ],
}];

/**
 * 等后端 `GET /api/metrics`（属 **D35 语义层**）：分析页汇总卡与热门工具。
 *
 * 注意：四个工具名取自本机 `GET /api/tools` 的**真实** `tools[].name`
 * （2026-10-07 实测：registry 共 14 个工具，harness 侧为本项目自己封装的 `harness_*`），
 * 让示例数据至少与真实命名空间一致；**`count` 全是编的，不可当实测值引用**。
 */
export const MOCK_ANALYTICS_SUMMARY = {
  totalRequests: 1284,
  averageLatencyMs: 642,
  tokenUsage: '1.82M',
  successRate: '98.4%',
  popularTools: [
    { name: 'search_documents', count: 72 },
    { name: 'harness_list', count: 46 },
    { name: 'current_time', count: 24 },
    { name: 'inventory_query_inventory', count: 18 },
  ],
};
