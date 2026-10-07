import { client } from './client';
import type { ToolsResponse } from '../types/api';

export async function getTools(): Promise<ToolsResponse> {
  const { data } = await client.get<ToolsResponse>('/tools');
  return data;
}
