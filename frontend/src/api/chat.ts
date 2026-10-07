import { client } from './client';
import type { ChatResponse } from '../types/api';

export async function sendMessage(message: string, sessionId?: string): Promise<ChatResponse> {
  const { data } = await client.post<ChatResponse>('/chat', {
    message,
    ...(sessionId ? { session_id: sessionId } : {}),
  });
  return data;
}
