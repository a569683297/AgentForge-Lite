import { useEffect, useState } from 'react';
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
import type { HealthResponse, SessionItem } from './types/api';

export type PageKey = 'chat' | 'knowledge' | 'eval' | 'analytics' | 'tools' | 'trace' | 'retriever';

export default function App() {
  const [page, setPage] = useState<PageKey>('chat');
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string>();

  useEffect(() => {
    void getHealth().then(setHealth).catch(() => setHealth(null));
    void listSessions().then(setSessions).catch(() => setSessions([]));
  }, []);

  function selectSession(id: string) {
    setSelectedSessionId(id);
    setPage('chat');
  }

  function updateSession(id: string) {
    setSelectedSessionId(id);
    void listSessions().then(setSessions).catch(() => undefined);
  }

  function renderPage() {
    switch (page) {
      case 'knowledge': return <KnowledgePage />;
      case 'eval': return <EvalPage />;
      case 'analytics': return <AnalyticsPage />;
      case 'tools': return <ToolsPage />;
      case 'trace': return <TracePage />;
      case 'retriever': return <RetrieverPage />;
      case 'chat':
      default:
        return <ChatPage sessionId={selectedSessionId} onNewSession={() => setSelectedSessionId(undefined)} onSessionChange={updateSession} onOpenKnowledge={() => setPage('knowledge')} />;
    }
  }

  return <AppShell page={page} onPageChange={setPage} sessions={sessions} health={health} selectedSessionId={selectedSessionId} onSessionSelect={selectSession}>{renderPage()}</AppShell>;
}
