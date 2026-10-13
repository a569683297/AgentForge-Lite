import { useCallback, useEffect, useState } from 'react';
import { Navigate, Route, Routes, useLocation, useMatch, useNavigate } from 'react-router-dom';
import { getHealth } from './api/health';
import { listSessions } from './api/sessions';
import { AppShell } from './components/AppShell';
import { AnalyticsPage } from './pages/AnalyticsPage';
import { ChatPage } from './pages/ChatPage';
import { EvalPage } from './pages/EvalPage';
import { KnowledgePage } from './pages/KnowledgePage';
import { RetrieverPage } from './pages/RetrieverPage';
import { ToolsPage } from './pages/ToolsPage';
import { TracePage } from './pages/TracePage';
import { pageFromPath } from './lib/routes';
import type { HealthResponse, SessionItem } from './types/api';

export default function App() {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  // ---- 下面两行是本次改造的核心：页面 & 会话都从 URL 派生 ----
  // 改造前它们是 useState（纯内存态），刷新即丢 —— 一律回到对话台。
  // 现在 URL 是唯一真源：地址栏里还写着 /knowledge，刷新后反推出的就还是知识库页。
  const page = pageFromPath(pathname);
  const selectedSessionId = useMatch('/chat/:sessionId')?.params.sessionId;

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [sessions, setSessions] = useState<SessionItem[]>([]);

  useEffect(() => {
    void getHealth().then(setHealth).catch(() => setHealth(null));
    void listSessions().then(setSessions).catch(() => setSessions([]));
  }, []);

  // 新会话产生后刷新侧栏「最近会话」列表（ChatPage 拿到首个 session_id 时调用）
  const refreshSessions = useCallback(() => {
    void listSessions().then(setSessions).catch(() => undefined);
  }, []);

  // 点侧栏历史会话：push 进浏览器历史 —— 之后可以用「后退」回到之前那页
  function selectSession(id: string) {
    navigate(`/chat/${id}`);
  }

  return (
    <AppShell page={page} sessions={sessions} health={health} selectedSessionId={selectedSessionId} onSessionSelect={selectSession}>
      {/* 路由表：每个页面一个 URL。外壳（侧栏/顶栏）不重挂载，只换内容区 ——
          所以 <Routes> 放在 AppShell 的 children 里，而不是当 layout route 用 */}
      <Routes>
        {/* 根路径直接进对话台（replace：不在历史里留一条 "/"） */}
        <Route path="/" element={<Navigate to="/chat" replace />} />
        {/* 对话台两个地址：/chat = 新会话；/chat/:sessionId = 打开某个已有会话。
            会话 id 不在这里手动传 —— ChatPage 内部用 useParams 从 URL 取 */}
        <Route path="/chat" element={<ChatPage onSessionsRefresh={refreshSessions} />} />
        <Route path="/chat/:sessionId" element={<ChatPage onSessionsRefresh={refreshSessions} />} />
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="/eval" element={<EvalPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/tools" element={<ToolsPage />} />
        <Route path="/trace" element={<TracePage />} />
        <Route path="/retriever" element={<RetrieverPage />} />
        {/* 未知路径统一回对话台 */}
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
    </AppShell>
  );
}
