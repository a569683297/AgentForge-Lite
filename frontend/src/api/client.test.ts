import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearSession, saveSession, type AuthUser } from '../lib/auth';
import { authedFetch } from './client';

/**
 * `authedFetch` 的契约测试。
 *
 * 这个函数存在的唯一理由：**fetch 不经过 axios 拦截器**。
 * 2026-10-21 的 bug 就出在这里 —— 流式对话用裸 fetch 直发，
 * Authorization 头从头到尾没被加上，于是"页面全都正常、唯独发消息 401"。
 *
 * 所以这里最重要的一条断言是：**请求真的带上了 Bearer**。
 * 其余几条守的是"不该做的事"：未登录别发明文 `Bearer null`、
 * 非 401 别乱重放、没有 refresh 时别死磕。
 *
 * 为什么不在这里测"401 → 刷新 → 重放"的完整链路：
 *   刷新走的是 axios（`rawClient.post('/auth/refresh')`），而本文件把全局 fetch
 *   打桩了，axios 在 jsdom 里走 XHR —— 两个通道，mock 不到一起。
 *   硬测就得把整个 axios mock 掉，代价远大于收益：刷新逻辑
 *   `refreshOnce()` 与 axios 拦截器是**同一个函数**，不是两份实现。
 *   真正跨进程的验证交给后端集成测试与浏览器实测。
 */

const ADMIN: AuthUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'admin@agentforge.local',
  display_name: '管理员',
  role: 'admin',
  is_active: true,
  created_at: '2026-10-21T00:00:00Z',
  last_login_at: null,
};

function loginWith(accessToken: string, refreshToken = 'refresh-1') {
  saveSession({ access_token: accessToken, refresh_token: refreshToken, expires_in: 7200, user: ADMIN });
}

/** 只用到 status 与 body，不需要真的 Response 实例 */
function fakeResponse(status: number): Response {
  return { status, ok: status >= 200 && status < 300, body: null } as unknown as Response;
}

let seen: { url: string; auth: string | null; method: string }[] = [];
let queue: Response[] = [];

beforeEach(() => {
  clearSession();
  seen = [];
  queue = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      seen.push({ url: String(input), auth: headers.get('Authorization'), method: init.method ?? 'GET' });
      const response = queue.shift();
      if (!response) throw new Error(`测试没给 ${input} 预置响应`);
      return response;
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearSession();
});

describe('authedFetch 必须补齐鉴权', () => {
  it('带上 Authorization: Bearer <access_token>（2026-10-21 bug 的根因）', async () => {
    loginWith('access-1');
    queue.push(fakeResponse(200));

    await authedFetch('/api/chat/stream', { method: 'POST' });

    expect(seen).toHaveLength(1);
    expect(seen[0].auth).toBe('Bearer access-1');
    expect(seen[0].method).toBe('POST');
  });

  it('未登录时不发 "Bearer null"（那会让后端 401 得更莫名其妙）', async () => {
    queue.push(fakeResponse(200));

    await authedFetch('/api/public/health');

    expect(seen[0].auth).toBeNull();
  });

  it('保留调用方自己的 header，只追加 Authorization', async () => {
    loginWith('access-1');
    queue.push(fakeResponse(200));

    await authedFetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    const init = (fetch as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0][1];
    const headers = new Headers(init.headers);
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('Authorization')).toBe('Bearer access-1');
  });

  it('非 401 原样返回，不重放（422 是参数问题，重试一万次也一样）', async () => {
    loginWith('access-1');
    queue.push(fakeResponse(422));

    const response = await authedFetch('/api/chat/stream', { method: 'POST' });

    expect(response.status).toBe(422);
    expect(seen).toHaveLength(1);
  });

  it('401 且手上没有 refresh token → 不重放，把 401 交给调用方', async () => {
    // 停在 /login：forceLogout 会因"已经在登录页"提前返回，不去触发 jsdom 的导航报错
    window.history.replaceState(null, '', '/login');
    queue.push(fakeResponse(401));

    const response = await authedFetch('/api/chat/stream', { method: 'POST' });

    expect(response.status).toBe(401);
    expect(seen).toHaveLength(1); // 没有 refresh 可换，重放没有意义
  });
});
