import { client } from './client';
import type { ToolServerListResponse, ToolServerOut, ToolsResponse } from '../types/api';

export async function getTools(): Promise<ToolsResponse> {
  const { data } = await client.get<ToolsResponse>('/tools');
  return data;
}

// ────────────────────────────────────────────────────────────────
// 用户上传的工具 server
// ────────────────────────────────────────────────────────────────
export async function getToolServers(): Promise<ToolServerListResponse> {
  const { data } = await client.get<ToolServerListResponse>('/tool-servers');
  return data;
}

export interface UploadToolInput {
  file: File;
  /** 机器名：同时是工具名前缀，规则由后端校验（3-32 位小写/数字/下划线） */
  name: string;
  displayName?: string;
  description?: string;
}

/**
 * 上传 + 接入。
 *
 * 超时单独放宽到 60s：这个请求不只是"存个文件"，它要
 * 起一个容器 → 完成 MCP 握手 → 拉工具清单。默认 20s 在冷启动时可能不够，
 * 而超时后前端会显示失败、后端其实已经接入成功 —— 最难处理的那种不一致。
 */
export async function uploadToolServer(input: UploadToolInput): Promise<ToolServerOut> {
  const formData = new FormData();
  formData.append('file', input.file);
  formData.append('name', input.name);
  formData.append('display_name', input.displayName ?? '');
  formData.append('description', input.description ?? '');
  const { data } = await client.post<ToolServerOut>('/tool-servers', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60_000,
  });
  return data;
}

export async function reconnectToolServer(id: string): Promise<ToolServerOut> {
  const { data } = await client.post<ToolServerOut>(`/tool-servers/${id}/reconnect`);
  return data;
}

export async function setToolServerStatus(
  id: string,
  status: 'active' | 'disabled',
): Promise<ToolServerOut> {
  const { data } = await client.patch<ToolServerOut>(`/tool-servers/${id}`, { status });
  return data;
}

export async function deleteToolServer(id: string): Promise<void> {
  await client.delete(`/tool-servers/${id}`);
}

/**
 * 试跑一个已上传的工具。
 *
 * 返回的是工具**原始输出字符串** —— 不包装、不截断。
 * 用户要判断的是"工具回了什么"，我们替他美化只会挡住问题。
 * 工具内部报错也走 `output`（execute_tool 的契约：失败也是文本），不是 HTTP 错误。
 */
export async function testTool(
  id: string,
  tool: string,
  args: Record<string, unknown>,
): Promise<{ tool: string; output: string }> {
  const { data } = await client.post<{ tool: string; output: string }>(
    `/tool-servers/${id}/call`,
    { tool, arguments: args },
    { timeout: 60_000 },
  );
  return data;
}
