import { client } from './client';

/**
 * 用户画像接口（前端唯一出口）
 * ============================
 * 三个方法对应后端的三个端点。**字段定义从后端来**（`fields`），
 * 前端不硬编码 "称呼/职业/技术栈/回答风格" —— 后端加一个字段时前端不用改代码。
 */

export interface ProfileField {
  key: string;
  label: string;
  max_length: number;
  hint: string;
}

export interface ProfileFact {
  id: number;
  kind: string;
  text: string;
  confidence: number;
  source_session_id: string | null;
  created_at: string | null;
}

export interface ProfilePayload {
  explicit: Record<string, string>;
  facts: ProfileFact[];
  fields: ProfileField[];
  fact_kinds: string[];
}

export async function getProfile(): Promise<ProfilePayload> {
  const { data } = await client.get<ProfilePayload>('/profile');
  return data;
}

/**
 * 保存显式字段。
 *
 * ⚠ 必须**提交全部字段**（包括用户清空的那些，传空串）：
 *   后端是 PATCH 语义 ——「没传」= 保持不变，「传空串」= 删除。
 *   只提交改动过的字段，会让"清空输入框"永远不生效。
 */
export async function patchProfile(values: Record<string, string>): Promise<Record<string, string>> {
  const { data } = await client.patch<{ explicit: Record<string, string> }>('/profile', values);
  return data.explicit;
}

export async function deleteProfileFact(id: number): Promise<void> {
  await client.delete(`/profile/facts/${id}`);
}
