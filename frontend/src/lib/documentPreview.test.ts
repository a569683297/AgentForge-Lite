import { describe, expect, it } from 'vitest';
import { flattenPreviewChunks, getDocumentKind } from './documentPreview';

describe('getDocumentKind', () => {
  it('maps supported document extensions to preview modes', () => {
    expect(getDocumentKind('policy.md')).toBe('markdown');
    expect(getDocumentKind('report.pdf')).toBe('pdf');
    expect(getDocumentKind('handbook.docx')).toBe('word');
    expect(getDocumentKind('notes.txt')).toBe('text');
  });
});

describe('flattenPreviewChunks', () => {
  it('keeps chunk order and page references in extracted text', () => {
    expect(flattenPreviewChunks([
      { chunk_index: 1, content: '第二段', page_ref: 'p.2' },
      { chunk_index: 0, content: '第一段', page_ref: 'p.1' },
    ])).toBe('【p.1】\n第一段\n\n【p.2】\n第二段');
  });
});
