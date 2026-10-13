import { client } from './client';
import { drainSseFrames } from '../lib/sse';
import type { ChatResponse, ChatStreamEvent } from '../types/api';

export async function sendMessage(message: string, sessionId?: string, modelId?: string | null): Promise<ChatResponse> {
  const { data } = await client.post<ChatResponse>('/chat', {
    message,
    ...(sessionId ? { session_id: sessionId } : {}),
    // 不传 model_id = 默认链路（主通道 + 失败降级）；传 = 锁定该模型（S2 语义）
    ...(modelId ? { model_id: modelId } : {}),
  });
  return data;
}

/**
 * 流式对话：`POST /api/chat/stream`，逐帧回调事件（M6 优化②）。
 *
 * 为什么用 fetch + ReadableStream 手写解析（而不是：
 *   - EventSource：只能发 GET、带不了请求体；
 *   - axios：浏览器端 XHR 拿不到流式响应体（responseType:'stream' 只在 Node 有效）。
 * 帧的切分逻辑在 `lib/sse.ts`（纯函数、单独测过"分块边界"）。
 *
 * 错误分两段处理 —— 这是 SSE 的固有限制，两条路径都不能漏：
 *   - **开流前**的错误走 HTTP 状态码（如 422 校验失败）→ 这里 throw；
 *   - **开流后**后端已发 200，错误只能以 `error` 事件送达 → 由 onEvent 回调转交
 *     （调用方必须监听，不能只看 HTTP 状态码）。
 */
export async function sendMessageStream(
  message: string,
  sessionId: string | undefined,
  onEvent: (event: ChatStreamEvent) => void,
  modelId?: string | null,
): Promise<void> {
  const response = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      ...(sessionId ? { session_id: sessionId } : {}),
      // 不传 model_id = 默认链路（主通道 + 失败降级）；传 = 锁定该模型（S2 语义）
      ...(modelId ? { model_id: modelId } : {}),
    }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await readErrorMessage(response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { payloads, rest } = drainSseFrames(buffer);
    buffer = rest;
    for (const payload of payloads) {
      // 解析失败按"契约违反"处理：直接抛（宁可红，不要静默少事件）——
      // 后端帧必然是合法 JSON，坏帧意味着有中间产物在篡改数据
      onEvent(JSON.parse(payload) as ChatStreamEvent);
    }
  }
}

/** 开流前的错误响应 → 可读信息（FastAPI 的 detail 优先，交给用户看） */
async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
  } catch {
    // 响应体不是 JSON（如网关的错误页）—— 落到通用文案
  }
  return `流式对话请求失败（HTTP ${response.status}）`;
}
