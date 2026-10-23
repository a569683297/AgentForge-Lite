// 路由真源：URL ←→ 页面 key 的双向映射
// ============================================================
// 这个文件很小，但它是本次改造的地基：让「当前在哪一页」**有且只有一个真源** —— URL。
//
// 为什么必须这样做（问题的根因）：
//   改造前 App.tsx 用 useState 记当前页（page='chat'），地址栏永远是 "/"。
//   刷新 = React 整个重新挂载 = useState 回到初始值 → 不管你在哪一页，
//   F5 之后一律回到对话台。因为「你在哪一页」这个事实**从来没写进 URL**，
//   浏览器无从保留它。
//
//   现在：翻页 = 换 URL（navigate('/knowledge')），
//        渲染 = 从 URL 反推页面（pageFromPath）。
//   刷新时浏览器重新加载同一个 URL → 反推出的还是同一页 → 自然「停留」。
//
// 附带获得的能力（不用额外写代码）：
//   - 浏览器前进 / 后退可用（history 由 router 接管）
//   - 链接可分享（把 /eval 发给别人，对方打开就是评测页）
//   - 刷新子路径不 404 —— nginx 的 SPA 兜底（try_files ... /index.html）已就位

export type PageKey = 'chat' | 'knowledge' | 'profile' | 'eval' | 'tools' | 'trace' | 'retriever' | 'admin';

/** 页面 → URL 路径（侧栏导航用它跳转） */
export const PAGE_PATHS: Record<PageKey, string> = {
  chat: '/chat',
  knowledge: '/knowledge',
  // 我的画像（2026-10-21）：所有登录用户可见，紧挨着对话台 ——
  // 它直接影响对话效果，放在"设置类"页面附近会让人找不到
  profile: '/profile',
  eval: '/eval',
  tools: '/tools',
  trace: '/trace',
  retriever: '/retriever',
  admin: '/admin',
};

/**
 * 页面 → 所需权限点。
 *
 * **这是"哪个页面要什么权限"的唯一出处**：侧栏过滤（AppShell）与路由守卫（App）
 * 都读这张表。分成两份写的后果是"菜单藏了但地址栏能进"或反过来 ——
 * 而这两种不一致都不会报错，只会让人以为权限没生效。
 *
 * 权限点本身来自后端（登录 / `/auth/me` 下发），前端不推导、不维护第二份
 * role → 权限的映射：那张表一旦与后端漂移，表现就是"入口显示了、点进去 403"。
 */
export const PAGE_PERMISSIONS: Record<PageKey, string> = {
  chat: 'chat.use',
  knowledge: 'doc.read',
  profile: 'profile.read',
  // 评测跑的是平台级语料，普通用户既没有语料管理权也不该看到全量分数
  eval: 'eval.read',
  // ⚠ 这里用 tools.manage（注册表管理），不是 tools.list：
  //   `tools.list` 是"对话时能拿到工具清单"，普通用户必须有，否则对话直接废掉；
  //   把两者合成一个权限点，就会逼出"要么普通用户能改全局工具配置，要么他不能对话"。
  tools: 'tools.manage',
  trace: 'trace.read',
  retriever: 'retriever.use',
  admin: 'admin.users',
};

/**
 * 历史路径别名（块 4：评测看板与分析页合并）。
 *
 * 合并前的 /analytics 已并入 /eval —— 旧书签 / 外部链接仍可能指向它。
 * App 里有一条 `<Route path="/analytics">` 负责把它们重定向到 /eval；
 * 这里再映射一份是为了**重定向发生前**的一瞬间：侧栏高亮直接从 URL 反推
 * （pageFromPath），没有别名的话 /analytics 会先命中默认值 'chat'（高亮闪一下对话台）。
 */
const PAGE_ALIASES: Record<string, PageKey> = { analytics: 'eval' };

const PAGE_KEYS = Object.keys(PAGE_PATHS) as PageKey[];

/**
 * URL 路径 → 页面 key（侧栏高亮、顶栏标题用它）。
 *
 * 只取第一段：/chat/:sessionId 也算 "chat" 页 —— 会话 id 属于「页内状态」，
 * 由 ChatPage 自己用 useParams 取，不在这层展开。
 * 未知路径回退 'chat'，与 App 里 <Route path="*"> 的重定向方向保持一致。
 */
export function pageFromPath(pathname: string): PageKey {
  const segment = pathname.split('/')[1] ?? '';
  if (segment in PAGE_ALIASES) return PAGE_ALIASES[segment];
  return PAGE_KEYS.includes(segment as PageKey) ? (segment as PageKey) : 'chat';
}
