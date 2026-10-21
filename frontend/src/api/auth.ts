import { client } from './client';
import type { AuthUser } from '../lib/auth';

/**
 * 账号接口（与后端 app/api/auth.py 一一对应）
 *
 * 这里刻意**不 import** lib/auth 的存储函数 —— 存 token 的动作由调用方
 * （AuthProvider / 拦截器）决定。理由：注册/登录接口的返回值会被"拦截器刷新"
 * 和"用户主动登录"两个场景共用，如果接口自己写存储，
 * 刷新时就会把 refresh 轮换后的新 token 悄悄覆盖成旧的那一份。
 */

export interface TokenPairResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

export async function login(email: string, password: string): Promise<TokenPairResponse> {
  const { data } = await client.post<TokenPairResponse>('/auth/login', { email, password });
  return data;
}

export async function register(
  email: string,
  password: string,
  displayName = '',
): Promise<TokenPairResponse> {
  const { data } = await client.post<TokenPairResponse>('/auth/register', {
    email,
    password,
    display_name: displayName,
  });
  return data;
}

export async function fetchMe(): Promise<AuthUser> {
  const { data } = await client.get<AuthUser>('/auth/me');
  return data;
}

export async function logout(refreshToken: string): Promise<void> {
  // 幂等接口：token 已烂也回 200。失败不抛 —— 前台无论如何都要清本地登录态
  await client.post('/auth/logout', { refresh_token: refreshToken });
}

export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  await client.post('/auth/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  });
}

/**
 * 申请重置链接。
 *
 * 返回里可能出现 `dev_link` —— **只有开发环境且未配 SMTP** 才会有，
 * 生产恒为 null。页面据此决定"要不要把链接直接显示出来"。
 */
export async function forgotPassword(email: string): Promise<{ message: string; dev_link: string | null }> {
  const { data } = await client.post<{ message: string; dev_link: string | null }>(
    '/auth/forgot-password',
    { email },
  );
  return data;
}

export async function resetPassword(token: string, newPassword: string): Promise<void> {
  await client.post('/auth/reset-password', { token, new_password: newPassword });
}
