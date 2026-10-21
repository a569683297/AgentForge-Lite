// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { clearSession, getSession, saveSession, subscribeSession, type AuthUser } from './auth';

/**
 * 「登录后界面不更新」这个 bug 的最小复现 + 回归防线。
 *
 * 2026-10-21 用户报的现象：
 *   点登录没反应，按 F5 才进得去；进去之后还发不了消息。
 * 一半原因在这里 —— App 用的是
 *     `useState(() => getSession())`    ← 只在首次挂载读一次
 * 而登录页调用 `saveSession()` 改的是 lib/auth 的模块变量。
 * 存储变了，React 毫不知情，路由守卫仍判"未登录"，于是把 /chat 推回 /login。
 *
 * 修法是把它换成 `useSyncExternalStore(subscribeSession, getSession)`。
 * 下面这个 Probe 组件就是 App 里那句订阅的等价物 —— 它红，App 就会红。
 *
 * 为什么值得专门写一个渲染测试而不是只测 subscribeSession：
 *   "订阅函数被调用了"和"界面上真的变了"是两件事。
 *   前者可能因为 React 的批处理/引用比较而**不产生渲染**（useSyncExternalStore
 *   要求快照引用稳定，实现若不满足会静默失效）。只有真渲染一次才作数。
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

/** App.tsx 里登录态那一行的等价物：订阅存储，而不是拷一份快照 */
function Probe() {
  const session = useSyncExternalStore(subscribeSession, getSession, getSession);
  return <div>{session ? `已登录:${session.user.email}:${session.user.role}` : '未登录'}</div>;
}

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeEach(async () => {
  clearSession();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<Probe />);
  });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  clearSession();
});

describe('登录态变化必须穿透到界面（2026-10-21 的 bug）', () => {
  it('登录成功后界面立刻变，不需要刷新页面', async () => {
    expect(container.textContent).toBe('未登录');

    await act(async () => {
      saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user: ADMIN });
    });

    // 旧实现（useState 快照）在这一行就会失败：仍显示"未登录"
    expect(container.textContent).toBe('已登录:admin@agentforge.local:admin');
  });

  it('退出登录后界面立刻回到未登录态', async () => {
    await act(async () => {
      saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user: ADMIN });
    });
    expect(container.textContent).toContain('已登录');

    await act(async () => {
      clearSession();
    });
    expect(container.textContent).toBe('未登录');
  });

  it('拦截器静默刷新 token 时不会把界面刷回未登录（快照引用必须稳定）', async () => {
    await act(async () => {
      saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user: ADMIN });
    });
    const before = container.textContent;

    // 只换 token，用户信息不变 —— 界面文本应当保持一致，不能闪
    const { updateTokens } = await import('./auth');
    await act(async () => {
      updateTokens({ access_token: 'a2', refresh_token: 'r2', expires_in: 7200 });
    });

    expect(container.textContent).toBe(before);
    expect(container.textContent).toBe('已登录:admin@agentforge.local:admin');
  });
});
