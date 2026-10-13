/**
 * SSE（Server-Sent Events）帧解析（M6 优化②）
 * ============================================
 * 后端 /api/chat/stream 用标准 SSE 编帧（`data: {...}\n\n`），
 * 这里负责把"网络到达的任意大小数据块"还原成"一条条完整事件"。
 *
 * 为什么不用浏览器内置的 EventSource（面试常问）：
 *   EventSource 只能发 GET、带不了请求体，而对话是 POST + {message, session_id}。
 *   所以"发请求 + 读流"全自己来（fetch + ReadableStream），协议层仍是标准 SSE ——
 *   解析器也按 SSE 规格写。
 *
 * 核心难点：**网络分块与帧边界不对齐**。
 *   一次 read() 可能拿到"半帧"，也可能拿到"两帧半"—— 解析器必须"带状态"：
 *   吃进累积缓冲，吐出完整帧 + 剩余残段（残段拼到下一块数据后面继续解析）。
 *   这条逻辑是纯函数，单独测试（sse.test.ts 里专门钉了"帧被切在中间"的用例）。
 */

export interface SseParseResult {
  /** 本次能确定切出的完整帧的数据段（已按规格处理：多余的空白不保留） */
  payloads: string[];
  /** 尚未凑齐的残段 —— 必须存起来，与下一块到达的数据拼接后继续解析 */
  rest: string;
}

/**
 * 从累积缓冲中切出所有**完整**的 SSE 帧，返回事件数据 + 剩余残段。
 *
 * 调用姿势（chat.ts 里的实际用法）：
 *     buffer += 新到的数据块;
 *     const { payloads, rest } = drainSseFrames(buffer);
 *     buffer = rest;
 *     payloads.forEach((p) => onEvent(JSON.parse(p)));
 */
export function drainSseFrames(buffer: string): SseParseResult {
  const payloads: string[] = [];
  let rest = buffer;

  // 帧分隔符是空行。按规格写 \n\n；兼容 \r\n\r\n —— 个别反向代理/中间件会改写换行
  const separator = /\r?\n\r?\n/;
  let match: RegExpExecArray | null;
  while ((match = separator.exec(rest))) {
    const frame = rest.slice(0, match.index);
    rest = rest.slice(match.index + match[0].length);
    const payload = readDataPayload(frame);
    if (payload !== null) payloads.push(payload);
  }
  return { payloads, rest };
}

/**
 * 从一帧的原始文本里取出 data 段。
 *
 * 按 SSE 规格：多行 `data:` 各贡献一行、用 `\n` 拼接；其他字段行
 * （event: / id: / retry: / 注释行）一律忽略 —— 本项目后端只发 `data:`，
 * 这里按"规格超集"实现：不用，但来了也不会解析错。
 *
 * 返回 null = 这一帧不产生事件（如只有注释行的心跳帧），**跳过而不是报错**。
 */
function readDataPayload(frame: string): string | null {
  const dataLines = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => {
      const value = line.slice('data:'.length);
      // 规格：`data:` 后恰好一个前导空格要被剥掉（没有空格也合法）
      return value.startsWith(' ') ? value.slice(1) : value;
    });
  if (!dataLines.length) return null;
  return dataLines.join('\n');
}
