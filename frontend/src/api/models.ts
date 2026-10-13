import { client } from './client';
import type { ModelInput, ModelItem, ModelTestInput, ModelTestResult } from '../types/api';

/**
 * 模型注册表 API（模型切换需求 S1 的后端，S3 前端接入）。
 *
 * 与对话链路的分工：这里管"**有哪些模型可选**"（列表 / CRUD / 连通性测试）；
 * "**这次用哪个**"走对话请求的 `model_id` 字段（见 api/chat.ts）。
 */

export async function listModels(): Promise<ModelItem[]> {
  const { data } = await client.get<{ models: ModelItem[] }>('/models');
  return data.models;
}

export async function createModel(input: ModelInput): Promise<ModelItem> {
  const { data } = await client.post<ModelItem>('/models', input);
  return data;
}

/**
 * 编辑自定义模型。
 *
 * ⚠ `api_key` 的语义（与后端约定，S1 定稿）：**留空 / 不传 = 不修改**。
 * 编辑表单只回显打码值，所以"提交空串"绝不能理解为"清空 key"——
 * 否则每次改个名字都会把 key 毁掉。
 */
export async function updateModel(id: string, patch: ModelInput): Promise<ModelItem> {
  const { data } = await client.patch<ModelItem>(`/models/${id}`, patch);
  return data;
}

export async function deleteModel(id: string): Promise<void> {
  await client.delete(`/models/${id}`);
}

/** 对表单里**还没保存**的配置做连通性测试（配置弹窗的「测试连接」按钮） */
export async function testModel(input: ModelTestInput): Promise<ModelTestResult> {
  const { data } = await client.post<ModelTestResult>('/models/test', input);
  return data;
}

/** 对**已保存**的模型做连通性测试 */
export async function testSavedModel(id: string): Promise<ModelTestResult> {
  const { data } = await client.post<ModelTestResult>(`/models/${id}/test`);
  return data;
}
