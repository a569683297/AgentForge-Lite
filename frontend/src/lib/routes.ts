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

export type PageKey = 'chat' | 'knowledge' | 'eval' | 'analytics' | 'tools' | 'trace' | 'retriever';

/** 页面 → URL 路径（侧栏导航用它跳转） */
export const PAGE_PATHS: Record<PageKey, string> = {
  chat: '/chat',
  knowledge: '/knowledge',
  eval: '/eval',
  analytics: '/analytics',
  tools: '/tools',
  trace: '/trace',
  retriever: '/retriever',
};

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
  return PAGE_KEYS.includes(segment as PageKey) ? (segment as PageKey) : 'chat';
}
