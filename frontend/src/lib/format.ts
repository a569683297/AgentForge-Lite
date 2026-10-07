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
  arguments: Record<string, unknown>;
  result: string;
  failed: boolean;
}

function parseArguments(payload: ToolCallPayload): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(payload.function.arguments || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

export function pairToolMessages(messages: MessageItem[]): PairedToolCall[] {
  const pairs: PairedToolCall[] = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role !== 'assistant' || !message.tool_calls?.length) continue;
    const resultMessage = messages.slice(index + 1).find((candidate) => candidate.role === 'tool');
    const result = resultMessage?.content ?? '未返回结果';
    pairs.push(...message.tool_calls.map((call) => ({
      name: call.function.name,
      arguments: parseArguments(call),
      result,
      failed: result.includes('执行失败') || result.includes('未知工具'),
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
