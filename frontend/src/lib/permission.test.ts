import { beforeEach, describe, expect, it } from 'vitest';
import { clearSession, hasPermission, hasPermissionOf, saveSession, type AuthUser } from './auth';

/**
 * 权限判定的契约测试
 * ==================
 * 这里的每一条都对应一种"看起来能用、其实在越权或误伤"的状态。
 * 最关键的两条：
 *
 * ① **不看 role，只看 permissions**
 *    前端如果按 role 自己推导，"角色改了但前端还是旧逻辑"就成了必然 ——
 *    而两边的表一旦漂移，表现是"菜单显示了出来但点进去 403"。
 *
 * ② **"缺失"与"空数组"是两件事**
 *    缺失 = 升级前的老 session，清单还没拿到 → 先放行（真实权限由后端拦）
 *    空数组 = 后端明确说"这个人没有任何权限" → 必须拦住
 *    把两者合并成一个兜底，就会在"角色配错"时静默放行整个应用。
 */

const BASE: AuthUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'u@example.com',
  display_name: '用户',
  role: 'user',
  is_active: true,
  created_at: '2026-10-23T00:00:00Z',
  last_login_at: null,
  permissions: ['chat.use', 'doc.read', 'profile.read'],
};

const ADMIN: AuthUser = {
  ...BASE,
  id: '22222222-2222-2222-2222-222222222222',
  email: 'a@example.com',
  role: 'admin',
  // 注意：必须显式写清单。若只改 role 而不给 admin.users，
  // 判定会（正确地）返回 false —— 前端只看清单，不看 role。
  permissions: ['chat.use', 'doc.read', 'profile.read', 'admin.users'],
};

function loginAs(user: AuthUser) {
  saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user });
}

beforeEach(() => {
  clearSession();
  localStorage.clear();
});

describe('hasPermissionOf：按后端下发的清单判断', () => {
  it('清单里有 → true', () => {
    expect(hasPermissionOf(BASE, 'chat.use')).toBe(true);
    expect(hasPermissionOf(BASE, 'doc.read')).toBe(true);
  });

  it('清单里没有 → false', () => {
    expect(hasPermissionOf(BASE, 'admin.users')).toBe(false);
    expect(hasPermissionOf(BASE, 'eval.read')).toBe(false);
  });

  it('不看 role：admin 角色但清单里没有该权限，仍然拒绝', () => {
    // 这条是"前端不自己按 role 推导"的守门用例。
    // 若实现里写了 `user.role === 'admin' || permissions.includes(...)`，这里会红。
    const demoted = { ...ADMIN, permissions: ['chat.use'] };
    expect(hasPermissionOf(demoted, 'admin.users')).toBe(false);
  });

  it('role 是 user 但清单里给了管理权限，也照给（以清单为准）', () => {
    const elevated = { ...BASE, permissions: ['admin.users'] };
    expect(hasPermissionOf(elevated, 'admin.users')).toBe(true);
  });

  it('权限清单缺失（升级前的老 session）→ 先放行，不锁死界面', () => {
    const legacy: AuthUser = { ...BASE, permissions: undefined };
    expect(hasPermissionOf(legacy, 'admin.users')).toBe(true);
  });

  it('空数组表示"明确没有权限"→ 一律拒绝', () => {
    // 与上一条的区别就是这整个权限体系的安全底线：
    // 缺字段是"不知道"，空数组是"知道，且答案是没有"。
    const nothing: AuthUser = { ...BASE, permissions: [] };
    expect(hasPermissionOf(nothing, 'chat.use')).toBe(false);
  });

  it('未登录（null / undefined）→ 一律 false', () => {
    expect(hasPermissionOf(null, 'chat.use')).toBe(false);
    expect(hasPermissionOf(undefined, 'chat.use')).toBe(false);
  });
});

describe('hasPermission：读当前登录态', () => {
  it('登录前没有权限，登录后按清单生效', () => {
    expect(hasPermission('chat.use')).toBe(false);
    loginAs(BASE);
    expect(hasPermission('chat.use')).toBe(true);
    expect(hasPermission('admin.users')).toBe(false);
  });

  it('退出登录后再次变为无权限', () => {
    loginAs(ADMIN);
    expect(hasPermission('admin.users')).toBe(true);
    clearSession();
    expect(hasPermission('admin.users')).toBe(false);
  });
});
