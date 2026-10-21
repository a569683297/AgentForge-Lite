import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearSession,
  getAccessToken,
  getSession,
  isAccessExpired,
  isAdmin,
  saveSession,
  subscribeSession,
  updateTokens,
  type AuthUser,
} from './auth';

/**
 * 登录态存储的契约测试。
 *
 * 为什么这几条必须测：`useSyncExternalStore` 对快照有**硬性引用要求** ——
 * getSnapshot 在"没变"时必须返回同一个对象，否则 React 会判定每次渲染都是新值，
 * 轻则无限重渲染、重则 "Maximum update depth exceeded" 白屏。
 * 这个约束看不见摸不着，只能靠测试钉住。
 *
 * 另一个重点：`subscribeSession` 的通知必须真的发出去。
 * 2026-10-21 的 bug 就出在这里 —— 写进了存储却没通知订阅者，
 * 表现为"登录后不跳转，F5 才进得去"。
 */

const KEY = 'af-auth';

const ADMIN: AuthUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'admin@agentforge.local',
  display_name: '管理员',
  role: 'admin',
  is_active: true,
  created_at: '2026-10-21T00:00:00Z',
  last_login_at: null,
};

const PLAIN: AuthUser = { ...ADMIN, id: '22222222-2222-2222-2222-222222222222', email: 'u@example.com', role: 'user' };

function loginAs(user: AuthUser) {
  saveSession({ access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 7200, user });
}

beforeEach(() => {
  clearSession();
  localStorage.clear();
});

describe('读写登录态', () => {
  it('saveSession 同时写内存与 localStorage', () => {
    loginAs(ADMIN);
    expect(getAccessToken()).toBe('access-1');
    expect(JSON.parse(localStorage.getItem(KEY) ?? '{}').refreshToken).toBe('refresh-1');
  });

  it('clearSession 把两处都清干净', () => {
    loginAs(ADMIN);
    clearSession();
    expect(getSession()).toBeNull();
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it('结构不全的脏数据按未登录处理（localStorage 是用户可改的）', () => {
    loginAs(ADMIN);
    const dirty = JSON.stringify({ accessToken: 'only-access' }); // 缺 refreshToken / user
    localStorage.setItem(KEY, dirty);
    // 走 storage 同步路径，让 readFromDisk 真正重新解析一次
    window.dispatchEvent(new StorageEvent('storage', { key: KEY, newValue: dirty }));
    expect(getSession()).toBeNull();
  });

  it('跨标签页同步：别的标签页退出登录，本页也跟着掉线', () => {
    loginAs(ADMIN);
    const listener = vi.fn();
    const unsubscribe = subscribeSession(listener);

    // 模拟另一个标签页调了 clearSession：localStorage 被删 + storage 事件到达
    localStorage.removeItem(KEY);
    window.dispatchEvent(new StorageEvent('storage', { key: KEY, newValue: null }));

    expect(getSession()).toBeNull();
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
  });

  it('无关的 storage 键不会扰动登录态', () => {
    loginAs(ADMIN);
    window.dispatchEvent(new StorageEvent('storage', { key: 'some-other-key', newValue: 'x' }));
    expect(getSession()?.user.email).toBe(ADMIN.email);
  });

  it('display 的用户信息能取到，但权限判定看 role 字段', () => {
    loginAs(ADMIN);
    expect(getSession()?.user.email).toBe(ADMIN.email);
    expect(isAdmin()).toBe(true);
    loginAs(PLAIN);
    expect(isAdmin()).toBe(false);
  });
});

describe('快照引用稳定性（useSyncExternalStore 的硬约束）', () => {
  it('不变时两次 getSession 返回同一个对象', () => {
    loginAs(ADMIN);
    expect(getSession()).toBe(getSession());
  });

  it('写入后引用必须变化，否则视图不会更新', () => {
    loginAs(ADMIN);
    const before = getSession();
    updateTokens({ access_token: 'access-2', refresh_token: 'refresh-2', expires_in: 7200 });
    expect(getSession()).not.toBe(before);
    expect(getAccessToken()).toBe('access-2');
  });

  it('updateTokens 必须覆盖 refresh —— 后端是轮换的，旧值已作废', () => {
    loginAs(ADMIN);
    updateTokens({ access_token: 'access-2', refresh_token: 'refresh-2', expires_in: 7200 });
    expect(getSession()?.refreshToken).toBe('refresh-2');
    // user 没传时要保留，不能把用户信息洗掉
    expect(getSession()?.user.email).toBe(ADMIN.email);
  });
});

describe('订阅通知（2026-10-21 修复的那个 bug）', () => {
  it('saveSession 会通知订阅者 —— 登录页写完，App 立刻知道', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeSession(listener);
    loginAs(ADMIN);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('clearSession 会通知订阅者 —— 退出/被踢后路由守卫立刻生效', () => {
    loginAs(ADMIN);
    const listener = vi.fn();
    const unsubscribe = subscribeSession(listener);
    clearSession();
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('updateTokens（拦截器静默刷新）也会通知 —— 否则界面停在旧用户信息上', () => {
    loginAs(ADMIN);
    const listener = vi.fn();
    const unsubscribe = subscribeSession(listener);
    updateTokens({ access_token: 'a2', refresh_token: 'r2', expires_in: 7200 });
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('取消订阅后不再收到通知（避免组件卸载后 setState 报警告）', () => {
    const listener = vi.fn();
    subscribeSession(listener)();
    loginAs(ADMIN);
    expect(listener).not.toHaveBeenCalled();
  });

  it('多个订阅者都会被通知；某个订阅者抛错不影响其它订阅者，也不污染调用方', () => {
    const good = vi.fn();
    const bad = vi.fn(() => {
      throw new Error('订阅者自己的错');
    });
    // 顺手把 console.error 静音，避免测试输出里出现"预期内的报错"
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const unsubscribeGood = subscribeSession(good);
    const unsubscribeBad = subscribeSession(bad);

    // 关键断言：saveSession 不抛。它若抛，登录页会显示"登录失败"，
    // 但凭证其实已经写进 localStorage 了 —— 状态与提示彻底对不上。
    expect(() => loginAs(ADMIN)).not.toThrow();
    expect(bad).toHaveBeenCalledTimes(1);
    expect(good).toHaveBeenCalledTimes(1); // 排在抛错者之后，仍然收到了通知
    expect(getAccessToken()).toBe('access-1'); // 存储写入没有被回滚

    unsubscribeGood();
    unsubscribeBad();
    spy.mockRestore();
  });
});

describe('过期判定', () => {
  it('expires_in 会提前 60 秒视为过期', () => {
    saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 30, user: ADMIN });
    // 30 秒 - 60 秒 → 夹到 0，立即算过期
    expect(isAccessExpired()).toBe(true);
  });

  it('两小时的有效期现在不该过期', () => {
    loginAs(ADMIN);
    expect(isAccessExpired()).toBe(false);
  });

  it('没有登录态时视为过期', () => {
    expect(isAccessExpired()).toBe(true);
  });
});
