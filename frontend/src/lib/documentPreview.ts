import type { DocumentChunkPreview } from '../types/api';

export type DocumentKind = 'markdown' | 'pdf' | 'word' | 'text' | 'unknown';

export function getDocumentKind(filename: string): DocumentKind {
  const extension = filename.split('.').pop()?.toLowerCase();
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  if (extension === 'pdf') return 'pdf';
  if (extension === 'doc' || extension === 'docx') return 'word';
  if (extension === 'txt') return 'text';
  return 'unknown';
}

export function flattenPreviewChunks(chunks: DocumentChunkPreview[]): string {
  return [...chunks]
    .sort((left, right) => left.chunk_index - right.chunk_index)
    .map((chunk) => `${chunk.page_ref ? `【${chunk.page_ref}】\n` : ''}${chunk.content}`)
    .join('\n\n');
}
