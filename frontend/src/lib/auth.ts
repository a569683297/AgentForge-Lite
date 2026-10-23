/**
 * 登录态的本地存储（唯一出口）
 * ============================
 * 为什么单独一个模块而不是在各处 `localStorage.getItem('token')`：
 *   ① 键名只有一个出处 —— 改名字时不用全项目搜字符串；
 *   ② 读写时的**约定**（存什么、什么时候清）集中在一处，不会出现
 *      "登录页存了 access、拦截器只认 token"这种对不上的事故；
 *   ③ 测试里可以直接 import 这个模块打桩。
 *
 * 三个刻意的决定：
 *
 * 1. **access 与 refresh 分开存，且都放在 localStorage**
 *    放 localStorage 而不是 httpOnly Cookie：本项目前后端同源部署（nginx 反代 /api），
 *    两种都可行；选 localStorage 是因为「后端不需要为鉴权维护 Cookie 域/SameSite 策略」，
 *    代价是 **XSS 能读到 token**。这个代价必须写明：生产环境要么上 CSP 收紧脚本来源，
 *    要么改成 httpOnly Cookie + CSRF token。当前是本地/单机演示形态，接受这个取舍。
 *
 * 2. **过期时间也存**（expiresAt）
 *    前端需要在 token 过期**前**主动刷新，而不是等 401 再重试 ——
 *    后者会让用户看到一次失败（哪怕自动恢复了）。OAuth2 的 expires_in 就是干这个的。
 *
 * 3. **用户信息一起缓存**（user）
 *    刷新页面时不用先打一次 /me 才能画侧栏的用户名；但**权限判断不靠它** ——
 *    真正能不能做某件事由后端决定，前端只做「藏起不该显示的入口」。
 */

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: 'user' | 'admin' | string;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
  /**
   * 后端下发的权限点清单（`chat.use` / `eval.read` / `admin.users` …）。
   *
   * 为什么是可选：
   *   账号体系升级前存进 localStorage 的 session 没有这个字段。
   *   读到旧数据时不能崩，由启动时的 `/auth/me` 补齐（见 App.tsx）。
   */
  permissions?: string[];
}

export interface StoredSession {
  accessToken: string;
  refreshToken: string;
  /** access token 的过期时刻（毫秒时间戳） */
  expiresAt: number;
  user: AuthUser;
}

const KEY = 'af-auth';

let current: StoredSession | null = readFromDisk();

function readFromDisk(): StoredSession | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredSession;
    // 结构校验：localStorage 是用户可改的，脏数据不该让整个应用崩在启动阶段
    if (!parsed?.accessToken || !parsed?.refreshToken || !parsed?.user) return null;
    return parsed;
  } catch {
    return null;
  }
}

// ============================================================
// 订阅：让 React 能"感知"登录态变化
// ============================================================
// 修过的 bug（2026-10-21 实测暴露）：
//   登录成功后 `saveSession()` 只改了本模块的 `current` + localStorage，
//   而 App 里的 `authSession` 是 `useState(() => getSession())` —— 只在**挂载时**
//   读一次。于是登录后 App 仍认为"未登录"，路由守卫立刻把 /chat 又推回 /login，
//   用户看到的就是"点了登录没反应，按 F5 才进去"。
//   根因不是登录没成功，是**状态变化的传播只做了一半**（写进了存储，没通知订阅者）。
//
// 为什么用 useSyncExternalStore 而不是"在 LoginPage 里 setState 往上抛"：
//   登录态有 5 个写入点（登录、注册、刷新 token、改密、退出），其中两个发生在
//   组件树之外（axios 拦截器）。逐一回调会让"谁改了登录态"散落在各处；
//   订阅模型下所有写入点只需 `emit()`，视图层自动跟上。
//
// ⚠ 前提：`getSession()` 必须返回**稳定引用**（不变时不换新对象），
//   否则 useSyncExternalStore 每次比较都判定"变了" → 无限重渲染。
//   这里满足：`current` 是模块级变量，只在写入时才换引用。
const listeners = new Set<() => void>();

/** 订阅登录态变化（返回取消订阅函数）。引用保持稳定，可安全作为 useEffect 依赖。 */
export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function emit(): void {
  for (const listener of listeners) {
    // ⚠ 逐个 try/catch，不能让一个订阅者的异常往外冒：
    //   emit 是在 saveSession/clearSession **写完之后**调用的。此刻抛错会让调用方
    //   （登录页的 try/catch）误判"登录失败"并弹出错误提示 —— 而凭证其实已经落盘了，
    //   用户重试还会 409/重复登录，状态与提示彻底对不上。
    //   同理，中断循环会让排在后面的订阅者漏掉这次变更（视图停在旧状态）。
    try {
      listener();
    } catch (error) {
      console.error('[auth] 登录态订阅者处理变更时抛错（已忽略）', error);
    }
  }
}

/** 当前登录态（同步读，渲染期间可用）。 */
export function getSession(): StoredSession | null {
  return current;
}

export function getAccessToken(): string | null {
  return current?.accessToken ?? null;
}

export function getRefreshToken(): string | null {
  return current?.refreshToken ?? null;
}

export function getCurrentUser(): AuthUser | null {
  return current?.user ?? null;
}

/**
 * 写入登录态。
 *
 * `expiresIn` 是后端回的**剩余秒数**（不是绝对时间）—— 换算成本地时间戳时
 * 用的是这台机器的钟。前后端时钟不同步时这里会有偏差，所以拦截器里
 * **不把过期时间当唯一依据**：真正的判定仍以 401 为准，本地时间只用来"提前刷新"。
 */
export function saveSession(payload: {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user: AuthUser;
}): void {
  current = {
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
    // 提前 60 秒视为过期：给"刷新请求在路上"的那一秒留余量
    expiresAt: Date.now() + Math.max(payload.expires_in - 60, 0) * 1000,
    user: payload.user,
  };
  localStorage.setItem(KEY, JSON.stringify(current));
  emit();
}

/** 只换 token 对（刷新时用，用户信息保持不变）。 */
export function updateTokens(payload: {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user?: AuthUser;
}): void {
  const user = payload.user ?? current?.user;
  if (!user) return;
  saveSession({ ...payload, user });
}

export function updateUser(user: AuthUser): void {
  if (!current) return;
  current = { ...current, user };
  localStorage.setItem(KEY, JSON.stringify(current));
  emit();
}

export function clearSession(): void {
  current = null;
  localStorage.removeItem(KEY);
  emit();
}

// ---- 多标签页同步 ----
// storage 事件只在**别的标签页**改动 localStorage 时触发（本页改的不触发，靠上面的 emit）。
// 没有它的话：A 标签页退出登录，B 标签页仍拿着已吊销的 refresh token 继续用，
// 直到下一次 401 才被踢 —— 中间这段时间它以为自己还登录着（并会不断重试刷新）。
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    // key === null 表示调了 localStorage.clear()，也要跟着同步
    if (event.key !== KEY && event.key !== null) return;
    current = readFromDisk();
    emit();
  });
}

/** access token 是否已到"该刷新"的时刻（不是"一定失效"）。 */
export function isAccessExpired(): boolean {
  if (!current) return true;
  return Date.now() >= current.expiresAt;
}

export function isAdmin(): boolean {
  return current?.user?.role === 'admin';
}

// ============================================================
// 权限判定
// ============================================================
/**
 * 某个用户是否拥有权限点（纯函数，不读模块状态）。
 *
 * 数据来源是**后端下发的 `permissions`**（登录 / 刷新 / GET /auth/me 三处都带）。
 * 前端**不再根据 role 自己推导**——这是「权限由后端把控」的关键：
 * 如果前端也维护一份 role → 权限的映射，两张表迟早漂移，而漂移那天不会报错，
 * 只会出现"菜单显示了但点进去 403"这种最难解释的故障。
 *
 * ⚠ `permissions` 缺失（升级前存下的旧 session）时返回 true：
 *   此刻是"还不知道"而不是"没有权限"。返回 false 会让侧栏瞬间全空（连对话台都没了），
 *   比多显示几个入口更难理解；而且真实能不能做仍由后端逐接口判定，
 *   这里只决定"显示什么"。启动时 `/auth/me` 会把它补齐，此后一律以服务端清单为准。
 */
export function hasPermissionOf(user: AuthUser | null | undefined, permission: string): boolean {
  // 未登录 = 什么都不能做。这里不能和"已登录但清单还没到"合并成一个兜底：
  // 前者是确定没有，后者是还不知道。
  if (!user) return false;
  const permissions = user.permissions;
  if (!Array.isArray(permissions)) return true;
  return permissions.includes(permission);
}

/** 用"当前登录态"做权限判定（给路由守卫用）。渲染期间请优先用 `hasPermissionOf`。 */
export function hasPermission(permission: string): boolean {
  return hasPermissionOf(current?.user ?? null, permission);
}
