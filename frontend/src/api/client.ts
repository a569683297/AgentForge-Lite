import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';
import {
  clearSession,
  getAccessToken,
  getRefreshToken,
  isAccessExpired,
  updateTokens,
} from '../lib/auth';

/**
 * 带 HTTP 状态码的接口错误。
 *
 * 为什么需要状态码：同一个「上传失败」有三种完全不同的应对 ——
 * 400 让用户换文件、409 说明这份文档已经在了（刷新列表即可）、500 才是真的故障。
 * 只保留 message 字符串时，页面只能靠「文案里有没有某个词」判断，改一个字就坏。
 */
export class ApiError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export const client = axios.create({
  baseURL: '/api',
  timeout: 20_000,
  headers: { 'Content-Type': 'application/json' },
});

// ============================================================
// 请求拦截：带上 Bearer
// ============================================================
// 为什么在拦截器里统一加，而不是每个 api 函数自己拼 header：
//   全项目 20 多个接口函数、7 个页面。漏一个就是"这个页面莫名 401"，
//   而且改 token 存储方式时要改 20 处。拦截器只有一处。
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken();
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`);
  }
  return config;
});

// ============================================================
// 响应拦截：401 → 静默刷新一次 → 重放原请求
// ============================================================
// 不做这件事的话，access token 每 2 小时到期一次，用户正在打字就会看到
// "登录已过期，请重新登录" —— 而他其实还有 7 天有效的 refresh token。
//
// ⚠ 三个必须处理的细节：
//   ① **刷新请求本身不能走这个拦截器** —— 否则刷新失败(401) 又会触发刷新，
//      递归到栈溢出。这里用一个裸 axios 实例发刷新请求。
//   ② **并发去重** —— 页面加载时常常同时发 3~4 个请求，全都 401 的话会并发
//      发起 3~4 次刷新。而 refresh 是**轮换**的：第一次成功后旧 refresh 作废，
//      后三次必然失败 → 用户被踢下线。所以共用同一个"正在刷新"的 Promise。
//   ③ **只重试一次** —— 用 `_retried` 标记，避免"刷新出的新 token 也被拒"时死循环。
interface RetriableConfig extends InternalAxiosRequestConfig {
  _retried?: boolean;
}

const rawClient = axios.create({ baseURL: '/api', timeout: 20_000 });

let refreshing: Promise<string | null> | null = null;

/** 用 refresh token 换一对新 token；失败返回 null（调用方负责踢下线）。 */
async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;
  try {
    const { data } = await rawClient.post<{
      access_token: string;
      refresh_token: string;
      expires_in: number;
      user?: never;
    }>('/auth/refresh', { refresh_token: refreshToken });
    // ⚠ 必须**覆盖** refresh token：后端是轮换的，旧的这份已经作废了。
    //   只更新 access 的话，下一次刷新会拿着已作废的旧 token → 401 → 踢下线。
    updateTokens(data);
    return data.access_token;
  } catch {
    return null;
  }
}

/** 同一个刷新只跑一次（并发去重，见上面 ②）。 */
function refreshOnce(): Promise<string | null> {
  refreshing ??= refreshAccessToken().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

/** 本地判定"access 已经该换了"时，先主动刷新一次再发请求。 */
async function ensureFreshToken(): Promise<void> {
  if (!isAccessExpired()) return;
  if (!getRefreshToken()) return;
  await refreshOnce();
}

client.interceptors.request.use(async (config: InternalAxiosRequestConfig) => {
  // 主动刷新：把 401 消灭在发生之前（见 auth.ts 里 expiresAt 的说明）
  //   /auth/ 下的接口**不参与** —— 登录/注册时本来就没有 token，
  //   对它们做"提前刷新"是纯浪费（还会拖慢登录）。
  if (!config.url?.startsWith('/auth/')) {
    await ensureFreshToken();
    const token = getAccessToken();
    if (token) config.headers.set('Authorization', `Bearer ${token}`);
  }
  return config;
});

/** 登录态彻底失效时的统一出口：清本地凭证并跳登录页（保留原地址，登录后能回来）。 */
function forceLogout(): void {
  clearSession();
  const { pathname, search } = window.location;
  if (pathname === '/login') return;
  const next = encodeURIComponent(`${pathname}${search}`);
  window.location.replace(`/login?next=${next}`);
}

client.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    if (!axios.isAxiosError(error)) return Promise.reject(error);

    const axiosError = error as AxiosError<{ detail?: string }>;
    const status = axiosError.response?.status;
    const config = axiosError.config as RetriableConfig | undefined;
    const url = config?.url ?? '';

    // ---- 401：先尝试刷新一次，再重放原请求 ----
    // 排除 /auth/ 自己的接口：登录密码错也是 401，那时不该去刷新
    if (status === 401 && config && !config._retried && !url.startsWith('/auth/')) {
      config._retried = true;
      const token = await refreshOnce();
      if (token) {
        config.headers.set('Authorization', `Bearer ${token}`);
        return client.request(config);
      }
      forceLogout();
    }

    // 刷新接口自己 401 = refresh token 也废了（过期/已登出/改密）
    if (status === 401 && url.startsWith('/auth/refresh')) {
      forceLogout();
    }

    const detail = axiosError.response?.data?.detail;
    const message = typeof detail === 'string' ? detail : axiosError.message;
    return Promise.reject(new ApiError(message, status));
  },
);

// ============================================================
// authedFetch：给"必须用 fetch"的接口补上鉴权
// ============================================================
// 修过的 bug（2026-10-21 实测暴露）：流式对话用 `fetch('/api/chat/stream')` 直发，
// 而 **fetch 不经过 axios 拦截器** —— Authorization 头不会自动加，401 也不会自动刷新。
// 后果是登录后"能看页面、不能发消息"：轮询类接口（axios）全绿，唯独发问 401。
// 这类 bug 的特点是**分工边界模糊**：看着是聊天坏了，实际是鉴权漏了一条通道。
//
// 为什么不让所有请求都走 fetch：axios 的拦截器/超时/取消已经够用，
// 只有"要读 ReadableStream"这一种场景 fetch 才不可替代（见 api/chat.ts 顶部注释）。
// 所以这里只补一个入口，且行为与拦截器**逐条对齐**（顺序不能改）：
//   ① 发之前先主动刷新（本地判定过期时）
//   ② 401 → 刷新 → 重放**一次**
//   ③ 刷新也失败 → 清本地并跳登录页
export async function authedFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const send = (): Promise<Response> => {
    // 每次都重新取 token：重放时要拿刷新后的新值，不能复用第一次的
    const headers = new Headers(init.headers);
    const token = getAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return fetch(input, { ...init, headers });
  };

  await ensureFreshToken();
  const first = await send();
  if (first.status !== 401) return first;

  // 重放前必须把第一次的响应体丢掉，否则连接不释放（浏览器的坑）
  void first.body?.cancel().catch(() => undefined);

  const token = await refreshOnce();
  if (!token) {
    forceLogout();
    // 返回原响应：调用方会照常抛"登录状态无效"。页面此刻正在跳走，
    // 这里不必再抛一个特殊异常去和它抢镜头。
    return first;
  }

  const second = await send();
  // 刷新出的新 token 仍被拒 = 账号被禁用 / 密钥轮换 → 直接踢，别再试
  if (second.status === 401) forceLogout();
  return second;
}
