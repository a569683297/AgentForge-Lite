import { describe, expect, it } from 'vitest';
import { drainSseFrames } from './sse';

describe('drainSseFrames（SSE 帧解析）', () => {
  it('一块数据里含多帧时，按到达顺序全部切出', () => {
    const result = drainSseFrames('data: {"type":"token","text":"你"}\n\ndata: {"type":"done"}\n\n');
    expect(result.payloads).toEqual(['{"type":"token","text":"你"}', '{"type":"done"}']);
    expect(result.rest).toBe('');
  });

  it('★ 帧被切在两个网络块之间：残段留给下一块拼回（分块边界的核心用例）', () => {
    // 第一块：半个帧（JSON 都被截断）
    const first = drainSseFrames('data: {"type":"token","text":"你');
    expect(first.payloads).toEqual([]);
    expect(first.rest).toBe('data: {"type":"token","text":"你');

    // 第二块到了：拼上残段，完整帧出现
    const second = drainSseFrames(`${first.rest}好"}\n\ndata: {"type":"done"}\n\n`);
    expect(second.payloads).toEqual(['{"type":"token","text":"你好"}', '{"type":"done"}']);
    expect(second.rest).toBe('');
  });

  it('只有半帧时：不产出事件、不丢数据', () => {
    const result = drainSseFrames('data: {"type":"token","text":"半');
    expect(result.payloads).toEqual([]);
    expect(result.rest).toBe('data: {"type":"token","text":"半');
  });

  it('兼容 \\r\\n\\r\\n 分隔符（少数代理会改写换行）', () => {
    const result = drainSseFrames('data: {"type":"meta"}\r\n\r\ndata: {"type":"done"}\r\n\r\n');
    expect(result.payloads).toEqual(['{"type":"meta"}', '{"type":"done"}']);
    expect(result.rest).toBe('');
  });

  it('忽略非 data 行与空帧（event: / 注释 / 空帧都不产生事件）', () => {
    const raw = ': keep-alive\n\nevent: ping\ndata: {"type":"token","text":"A"}\n\n\n\ndata: {"type":"done"}\n\n';
    const result = drainSseFrames(raw);
    expect(result.payloads).toEqual(['{"type":"token","text":"A"}', '{"type":"done"}']);
    expect(result.rest).toBe('');
  });

  it('多行 data: 按规格以换行拼接', () => {
    const result = drainSseFrames('data: 第一行\ndata: 第二行\n\n');
    expect(result.payloads).toEqual(['第一行\n第二行']);
  });

  it('data: 后没有空格也合法（剥掉恰好一个前导空格，不多剥）', () => {
    const result = drainSseFrames('data:{"type":"meta"}\n\ndata:  两个空格\n\n');
    expect(result.payloads).toEqual(['{"type":"meta"}', ' 两个空格']);
  });
});
