import { client } from './client';
import type { DocumentOut, DocumentPreviewOut } from '../types/api';

export async function listDocuments(limit = 50, offset = 0): Promise<DocumentOut[]> {
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
