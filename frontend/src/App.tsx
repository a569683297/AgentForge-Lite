import { useCallback, useEffect, useState } from 'react';
import { Navigate, Route, Routes, useLocation, useMatch, useNavigate } from 'react-router-dom';
import { getHealth } from './api/health';
import { listModels } from './api/models';
import { listSessions } from './api/sessions';
import { AppShell } from './components/AppShell';
import { AnalyticsPage } from './pages/AnalyticsPage';
import { ChatPage } from './pages/ChatPage';
import { EvalPage } from './pages/EvalPage';
import { KnowledgePage } from './pages/KnowledgePage';
import { RetrieverPage } from './pages/RetrieverPage';
import { ToolsPage } from './pages/ToolsPage';
import { TracePage } from './pages/TracePage';
import { rememberModel, resolveModelForSession, type ModelSelection } from './lib/modelMemory';
import { pageFromPath } from './lib/routes';
import type { HealthResponse, ModelItem, SessionItem } from './types/api';

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

  // ---- 模型切换（S3）：列表在 App 层加载一次，下拉（AppShell）与发问（ChatPage）共用 ----
  const [models, setModels] = useState<ModelItem[]>([]);
  const refreshModels = useCallback(() => {
    void listModels().then(setModels).catch(() => undefined);
  }, []);

  // 当前会话选中的模型。初值 & 会话切换时都从**会话级记忆**（localStorage）解析 ——
  // "各候选记各的 + 新会话继承最近选择"的语义在 lib/modelMemory.ts，这里只负责同步。
  const [selectedModelId, setSelectedModelId] = useState<ModelSelection>(() => resolveModelForSession(selectedSessionId));
  useEffect(() => {
    setSelectedModelId(resolveModelForSession(selectedSessionId));
  }, [selectedSessionId]);

  // 用户在下拉里切换：写记忆（该会话 + 最近选择）+ 更新界面选中态
  const handleModelSelect = useCallback((modelId: ModelSelection) => {
    rememberModel(selectedSessionId, modelId);
    setSelectedModelId(modelId);
  }, [selectedSessionId]);

  // ChatPage 在 done 时上报"本轮实际带上的选择" —— 用于**新会话建立后的补记**：
  // /chat 页里选了 X（只进了 last）→ 发问 → 会话 S 建立；把 X 补记到 S 名下，
  // 之后回到 S 时解析的就是 X 而不是"当时的 last"（可能已被别的会话改掉）。
  const handleModelUsed = useCallback((sessionId: string, modelId: string) => {
    rememberModel(sessionId, modelId);
  }, []);

  // 选中模型的"孤儿"守卫：列表刷新后，当前选中若已不在列表里（被删除 / 内置通道的 key
  // 被移除）→ 回退"默认"。models 为空不触发（加载失败 ≠ 全部被删，不能误伤）。
  // 窄边界：其它会话记忆里的孤儿 id 不在这里清 —— 它们会在"回到那个会话"时
  // 被同一守卫清掉；ModelSwitcher 对"未知模型"也有展示兜底，不存在崩溃路径。
  useEffect(() => {
    if (selectedModelId && models.length > 0 && !models.some((model) => model.id === selectedModelId)) {
      rememberModel(selectedSessionId, null);
      setSelectedModelId(null);
    }
  }, [models, selectedModelId, selectedSessionId]);

  useEffect(() => {
    void getHealth().then(setHealth).catch(() => setHealth(null));
    void listSessions().then(setSessions).catch(() => setSessions([]));
    refreshModels();
  }, [refreshModels]);

  // 新会话产生后刷新侧栏「最近会话」列表（ChatPage 拿到首个 session_id 时调用）
  const refreshSessions = useCallback(() => {
    void listSessions().then(setSessions).catch(() => undefined);
  }, []);

  // 点侧栏历史会话：push 进浏览器历史 —— 之后可以用「后退」回到之前那页
  function selectSession(id: string) {
    navigate(`/chat/${id}`);
  }

  return (
    <AppShell
      page={page}
      sessions={sessions}
      health={health}
      selectedSessionId={selectedSessionId}
      onSessionSelect={selectSession}
      models={models}
      selectedModelId={selectedModelId}
      onModelSelect={handleModelSelect}
      onModelsRefresh={refreshModels}
    >
      {/* 路由表：每个页面一个 URL。外壳（侧栏/顶栏）不重挂载，只换内容区 ——
          所以 <Routes> 放在 AppShell 的 children 里，而不是当 layout route 用 */}
      <Routes>
        {/* 根路径直接进对话台（replace：不在历史里留一条 "/"） */}
        <Route path="/" element={<Navigate to="/chat" replace />} />
        {/* 对话台两个地址：/chat = 新会话；/chat/:sessionId = 打开某个已有会话。
            会话 id 不在这里手动传 —— ChatPage 内部用 useParams 从 URL 取 */}
        <Route path="/chat" element={<ChatPage onSessionsRefresh={refreshSessions} selectedModelId={selectedModelId} onModelUsed={handleModelUsed} />} />
        <Route path="/chat/:sessionId" element={<ChatPage onSessionsRefresh={refreshSessions} selectedModelId={selectedModelId} onModelUsed={handleModelUsed} />} />
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
