import { client } from './client';
import type {
  TraceDetailResponse,
  TraceKind,
  TraceListResponse,
} from '../types/api';

/**
 * 最近的 trace 列表 —— 每行是一次提问 / 一次运行。
 *
 * 一次请求就把窗口内所有 span 拉回来了，由后端按 traceId 分组 ——
 * 前端不需要为"列表"和"每条 trace 的树"分别发请求。
 *
 * `kind` 在**后端**过滤（不是前端 filter）：窗口内 eval-judge 与
 * retriever_inspect 的数量远多于 run_agent，先截断后过滤会把对话挤掉。
 */
export async function getTraces(
  hours = 24,
  limit = 20,
  kind?: TraceKind,
): Promise<TraceListResponse> {
  const { data } = await client.get<TraceListResponse>('/traces', {
    params: { hours, limit, ...(kind ? { kind } : {}) },
  });
  return data;
}

/** 一条 trace 的完整 span 树（后端按 traceId 精确过滤，一次取全）。 */
export async function getTrace(traceId: string): Promise<TraceDetailResponse> {
  const { data } = await client.get<TraceDetailResponse>(`/traces/${traceId}`);
  return data;
}
