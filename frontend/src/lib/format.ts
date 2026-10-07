import type { EvalRunOut, MessageItem, ToolCallPayload } from '../types/api';

export type CitationPart =
  | { type: 'text'; value: string }
  | { type: 'citation'; value: string; valid: boolean };

export function extractCitationParts(answer: string, validIndexes: number[]): CitationPart[] {
  const valid = new Set(validIndexes);
  const parts: CitationPart[] = [];
  const citationPattern = /\[(\d+)\]/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = citationPattern.exec(answer))) {
    if (match.index > cursor) parts.push({ type: 'text', value: answer.slice(cursor, match.index) });
    const value = match[1];
    parts.push({ type: 'citation', value, valid: valid.has(Number(value)) });
    cursor = match.index + match[0].length;
  }
  if (cursor < answer.length) parts.push({ type: 'text', value: answer.slice(cursor) });
  return parts.length ? parts : [{ type: 'text', value: answer }];
}

export function latestByConfig<T extends Pick<EvalRunOut, 'id' | 'config_name'>>(runs: T[]): T[] {
  const seen = new Set<string>();
  const latest: T[] = [];
  for (const run of runs) {
    if (!seen.has(run.config_name)) {
      seen.add(run.config_name);
      latest.push(run);
    }
  }
  return latest;
}

export interface PairedToolCall {
  name: string;
  /**
   * `null` = **数据源根本没给入参**（如 `POST /api/chat` 只回工具名），
   * 与「入参是空对象 `{}`」是两件事，渲染时必须分开。
   */
  arguments: Record<string, unknown> | null;
  /** `null` = **数据源没给返回摘要**（同上）。 */
  result: string | null;
  /** 只在 `result` 存在时可判定；`result` 为 `null` 时恒为 `false`（**未知 ≠ 成功**）。 */
  failed: boolean;
}

/** 解析 `tool_calls[].function.arguments`（一个 JSON 字符串）。解析不出来返回 `null`（= 未知），不返回 `{}`。 */
function parseArguments(payload: ToolCallPayload): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(payload.function.arguments || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function isFailed(result: string): boolean {
  return result.includes('执行失败') || result.includes('未知工具');
}

export function pairToolMessages(messages: MessageItem[]): PairedToolCall[] {
  const pairs: PairedToolCall[] = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role !== 'assistant' || !message.tool_calls?.length) continue;
    const resultMessage = messages.slice(index + 1).find((candidate) => candidate.role === 'tool');
    // 找不到配对的 tool 消息 = 结果未知，落 null —— 不写「未返回结果」那种读起来像结论的文案
    const result = resultMessage?.content ?? null;
    pairs.push(...message.tool_calls.map((call) => ({
      name: call.function.name,
      arguments: parseArguments(call),
      result,
      failed: result != null && isFailed(result),
    })));
  }
  return pairs;
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function formatScore(value: number | null | undefined): string {
  return value == null ? '—' : value.toFixed(2);
}

export function formatPercent(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(2)}%`;
}
