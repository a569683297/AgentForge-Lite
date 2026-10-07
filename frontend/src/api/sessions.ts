import { client } from './client';
import type { MessageItem, SessionItem } from '../types/api';

export async function listSessions(limit = 20): Promise<SessionItem[]> {
  const { data } = await client.get<SessionItem[]>('/sessions', { params: { limit } });
  return data;
}

export async function getSessionMessages(sessionId: string): Promise<MessageItem[]> {
  const { data } = await client.get<MessageItem[]>(`/sessions/${sessionId}/messages`);
  return data;
}
