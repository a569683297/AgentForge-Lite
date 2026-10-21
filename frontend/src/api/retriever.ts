import { client } from './client';
import type {
  RetrieverOptions,
  RetrieverSearchRequest,
  RetrieverSearchResponse,
} from '../types/api';

/**
 * 检索检查器的可选项。
 *
 * 配置名、top-k 上限、默认值都由后端给 —— 前端硬编码一份的话，
 * 后端点改一处，页面就会拿一个不存在的配置名去请求，收到 400 而不是"选项里没有它"。
 */
export async function getRetrieverOptions(): Promise<RetrieverOptions> {
  const { data } = await client.get<RetrieverOptions>('/retriever/options');
  return data;
}

/**
 * 跑一次检索，拿回完整取舍过程。
 *
 * 用 POST 而不是 GET 的两个实际理由：
 *   ① query 可能很长，塞进 URL 会被中间层截断（nginx 默认 8k，但日志里也不好读）；
 *   ② 这个接口**有副作用**（会真的跑一次 embedding + 重排，约 1.3s），
 *      GET 在语义上是幂等只读，会被浏览器/代理缓存。
 */
export async function searchRetriever(
  body: RetrieverSearchRequest,
): Promise<RetrieverSearchResponse> {
  const { data } = await client.post<RetrieverSearchResponse>('/retriever/search', body);
  return data;
}
