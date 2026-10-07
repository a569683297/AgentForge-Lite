import { describe, expect, it } from 'vitest';
import { extractCitationParts, latestByConfig, pairToolMessages } from './format';

describe('extractCitationParts', () => {
  it('marks known and invalid citation references without changing normal text', () => {
    expect(extractCitationParts('答案 [1] 和 [9]。', [1])).toEqual([
      { type: 'text', value: '答案 ' },
      { type: 'citation', value: '1', valid: true },
      { type: 'text', value: ' 和 ' },
      { type: 'citation', value: '9', valid: false },
      { type: 'text', value: '。' },
    ]);
  });
});

describe('latestByConfig', () => {
  it('keeps the newest run for every evaluation configuration', () => {
    expect(latestByConfig([
      { id: 4, config_name: 'hybrid' },
      { id: 3, config_name: 'hybrid' },
      { id: 2, config_name: 'pure_vector' },
    ])).toEqual([
      { id: 4, config_name: 'hybrid' },
      { id: 2, config_name: 'pure_vector' },
    ]);
  });
});

describe('pairToolMessages', () => {
  it('pairs an assistant function call with the following tool result', () => {
    expect(pairToolMessages([
      { role: 'assistant', content: '', tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'search_documents', arguments: '{"query":"RAG"}' } }], created_at: '2026-01-01' },
      { role: 'tool', content: '命中 3 个片段', tool_calls: null, created_at: '2026-01-01' },
    ])).toEqual([
      { name: 'search_documents', arguments: { query: 'RAG' }, result: '命中 3 个片段', failed: false },
    ]);
  });

  it('keeps result null (unknown) when no tool message follows, instead of claiming success', () => {
    expect(pairToolMessages([
      { role: 'assistant', content: '', tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'current_time', arguments: '{}' } }], created_at: '2026-01-01' },
    ])).toEqual([
      { name: 'current_time', arguments: {}, result: null, failed: false },
    ]);
  });

  it('keeps arguments null (unknown) when the arguments string is not valid JSON', () => {
    expect(pairToolMessages([
      { role: 'assistant', content: '', tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'broken', arguments: '{not json' } }], created_at: '2026-01-01' },
      { role: 'tool', content: '执行失败', tool_calls: null, created_at: '2026-01-01' },
    ])).toEqual([
      { name: 'broken', arguments: null, result: '执行失败', failed: true },
    ]);
  });
});
