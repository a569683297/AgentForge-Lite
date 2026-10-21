import { client } from './client';
import type { AuthUser } from '../lib/auth';

/**
 * 管理员接口（用户管理 + 审计日志）
 *
 * 与后端 `app/api/admin.py` 一一对应。三个接口都要求调用者是 admin，
 * 非管理员会拿到 403（`ApiError.status === 403`）——
 * 页面据此显示"需要管理员权限"，而不是当成"加载失败"。
 */

export interface UserPage {
  total: number;
  items: AuthUser[];
}

export interface AuditItem {
  id: number;
  actor_id: string | null;
  actor_email: string;
  action: string;
  target_type: string;
  target_id: string;
  detail: Record<string, unknown> | null;
  ip: string;
  created_at: string;
}

export interface AuditPage {
  total: number;
  items: AuditItem[];
}

export async function listUsers(params: { limit?: number; offset?: number; keyword?: string } = {}): Promise<UserPage> {
  const { data } = await client.get<UserPage>('/admin/users', { params });
  return data;
}

export async function patchUser(
  userId: string,
  patch: { is_active?: boolean; role?: 'user' | 'admin' },
): Promise<AuthUser> {
  const { data } = await client.patch<AuthUser>(`/admin/users/${userId}`, patch);
  return data;
}

export async function listAuditActions(): Promise<string[]> {
  const { data } = await client.get<{ actions: string[] }>('/admin/audit/actions');
  return data.actions;
}

export async function listAudit(
  params: { limit?: number; offset?: number; action?: string } = {},
): Promise<AuditPage> {
  const { data } = await client.get<AuditPage>('/admin/audit', { params });
  return data;
}
