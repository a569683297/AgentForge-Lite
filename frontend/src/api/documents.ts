import { client } from './client';
import type { DocumentOut, DocumentPreviewOut } from '../types/api';

// limit 默认取后端上限 200：知识库列表是「全量拉取 + 前端分页/筛选」模式，
// 若用默认 50，文档超过 50 篇会被静默截断（第 51 篇起根本看不到）。
export async function listDocuments(limit = 200, offset = 0): Promise<DocumentOut[]> {
  const { data } = await client.get<DocumentOut[]>('/documents', { params: { limit, offset } });
  return data;
}

export async function uploadDocument(file: File): Promise<DocumentOut> {
  const formData = new FormData();
  formData.append('file', file);
  const { data } = await client.post<DocumentOut>('/documents', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function deleteDocument(id: string): Promise<void> {
  await client.delete(`/documents/${id}`);
}

export async function getDocumentPreview(id: string): Promise<DocumentPreviewOut> {
  const { data } = await client.get<DocumentPreviewOut>(`/documents/${id}/preview`);
  return data;
}
