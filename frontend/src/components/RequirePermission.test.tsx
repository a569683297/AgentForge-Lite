// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { clearSession, saveSession, updateUser, type AuthUser } from '../lib/auth';
import { RequirePermission } from './RequirePermission';

/**
 * 路由守卫的渲染测试
 * ==================
 * 这个文件只守一件事：**权限变化必须穿透到界面**。
 *
 * 为什么不能用"直接调 hasPermissionOf"代替：
 *   判定的纯函数可以全对，而守卫仍然用一次性快照渲染 —— 那样管理员被降级后
 *   页面会一直停在管理页，直到手动刷新。`useSyncExternalStore` 的价值就在
 *   "组件树外的写入也能触发重渲染"，这一点只有真渲染一次才能验证。
 *
 * 第二条守的是本次改造的原始诉求：
 *   改造前普通用户手输 `/admin` 能打开页面框架（只是接口全 403）。
 *   现在必须给出明确的"没有访问权限"，而不是一片空白表格。
 */

const BASE: AuthUser = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'u@example.com',
  display_name: '用户',
  role: 'user',
  is_active: true,
  created_at: '2026-10-23T00:00:00Z',
  last_login_at: null,
  permissions: ['chat.use'],
};

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

function renderGuarded(permission: string) {
  root.render(
    <MemoryRouter>
      <RequirePermission permission={permission}>
        <div>受保护内容</div>
      </RequirePermission>
    </MemoryRouter>,
  );
}

beforeEach(async () => {
  clearSession();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  clearSession();
  localStorage.clear();
});

describe('RequirePermission', () => {
  it('有权限时渲染子内容', async () => {
    saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user: BASE });
    await act(async () => renderGuarded('chat.use'));
    expect(container.textContent).toContain('受保护内容');
  });

  it('没有权限时显示 403 说明，而不是渲染页面', async () => {
    saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user: BASE });
    await act(async () => renderGuarded('admin.users'));

    expect(container.textContent).not.toContain('受保护内容');
    expect(container.textContent).toContain('没有访问权限');
    // 把缺的权限点显示出来 —— 用户才能跟管理员说清缺什么
    expect(container.textContent).toContain('admin.users');
  });

  it('权限在组件树外被更新后，界面自动放行（订阅生效）', async () => {
    saveSession({ access_token: 'a', refresh_token: 'r', expires_in: 7200, user: BASE });
    await act(async () => renderGuarded('admin.users'));
    expect(container.textContent).toContain('没有访问权限');

    // 模拟"管理员刚在后台把角色改成了 admin，前端 /auth/me 校准后写回"
    await act(async () => {
      updateUser({ ...BASE, role: 'admin', permissions: ['chat.use', 'admin.users'] });
    });

    // 旧实现（渲染时直接读 getSession() 一次）在这一行会失败：仍停在 403
    expect(container.textContent).toContain('受保护内容');
  });

  it('权限被收回后立刻变回 403', async () => {
    saveSession({
      access_token: 'a',
      refresh_token: 'r',
      expires_in: 7200,
      user: { ...BASE, role: 'admin', permissions: ['chat.use', 'admin.users'] },
    });
    await act(async () => renderGuarded('admin.users'));
    expect(container.textContent).toContain('受保护内容');

    await act(async () => {
      updateUser({ ...BASE, permissions: ['chat.use'] });
    });

    expect(container.textContent).toContain('没有访问权限');
  });
});
