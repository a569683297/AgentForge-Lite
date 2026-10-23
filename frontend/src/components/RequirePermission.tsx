import { useSyncExternalStore, type ReactNode } from 'react';
import { getSession, hasPermissionOf, subscribeSession } from '../lib/auth';
import { ForbiddenPage } from '../pages/ForbiddenPage';

/**
 * 路由级权限守卫
 * ==============
 * 补上"藏入口"之外的那一层：**地址栏直接输入也能拦住**。
 *
 * 改造前的状态（2026-10-23 排查确认）：
 *   侧栏按 role 过滤了菜单，但 `/admin` 这类路由没有任何守卫 ——
 *   普通用户手输 URL 能打开页面框架，只是所有接口都 403。
 *   那不是安全洞（后端拦住了），但用户看到的是一堆错误提示和空白表格，
 *   分不清"没权限"和"系统坏了"。
 *
 * 为什么用 `useSyncExternalStore` 而不是 `getSession()` 直接读：
 *   角色/权限会在**组件树之外**发生变化（登录、静默刷新、`/auth/me` 补齐清单）。
 *   直接读模块变量只在本次渲染那一瞬正确，之后不会重渲染 ——
 *   表现为"管理员被降级后，页面还停在管理页直到手动刷新"。
 *
 * ⚠ 这层不是安全边界：真正的权限判定在后端（每个接口的 `require_permission`）。
 *   前端只负责"不把走不通的路摆出来、走不通时说清原因"。
 */
export function RequirePermission({
  permission,
  children,
}: {
  permission: string;
  children: ReactNode;
}) {
  const session = useSyncExternalStore(subscribeSession, getSession, getSession);
  if (!hasPermissionOf(session?.user ?? null, permission)) {
    return <ForbiddenPage permission={permission} />;
  }
  return <>{children}</>;
}
