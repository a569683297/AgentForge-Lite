import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { Navigate, Route, Routes, useLocation, useMatch, useNavigate } from 'react-router-dom';
import { logout as logoutApi } from './api/auth';
import { getHealth } from './api/health';
import { listModels } from './api/models';
import { listSessions } from './api/sessions';
import { AppShell } from './components/AppShell';
import { ChangePasswordModal } from './components/ChangePasswordModal';
import { AdminPage } from './pages/AdminPage';
import { ChatPage } from './pages/ChatPage';
import { EvalPage } from './pages/EvalPage';
import { KnowledgePage } from './pages/KnowledgePage';
import { LoginPage } from './pages/LoginPage';
import { ProfilePage } from './pages/ProfilePage';
import { ResetPasswordPage } from './pages/ResetPasswordPage';
import { RetrieverPage } from './pages/RetrieverPage';
import { ToolsPage } from './pages/ToolsPage';
import { TracePage } from './pages/TracePage';
import { clearSession, getRefreshToken, getSession, subscribeSession } from './lib/auth';
import { rememberModel, resolveModelForSession, type ModelSelection } from './lib/modelMemory';
import { pageFromPath } from './lib/routes';
import type { HealthResponse, ModelItem, SessionItem } from './types/api';

export default function App() {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  // ---- 登录态：直接订阅 lib/auth 的存储，**不再自己拿 useState 镜像** ----
  // 修过的 bug（2026-10-21 实测暴露）：原来是
  //   `useState(() => getSession())` —— 只在**首次挂载**读一次。
  //   登录页里 saveSession() 写的是 lib/auth 的模块变量 + localStorage，
  //   App 这边毫不知情 → 仍判"未登录" → 路由守卫把 /chat 又推回 /login。
  //   表现为「点登录没反应，F5 才进去」。
  //
  // 为什么用 useSyncExternalStore 而不是用 Context 包一层：
  //   登录态的写入点有 5 处，其中「刷新 token」和「强制下线」发生在 axios 拦截器里，
  //   不在组件树内 —— 那些地方无法调用 setState。订阅模型对它们同样有效。
  // 不需要提前抽象。
  const authSession = useSyncExternalStore(subscribeSession, getSession, getSession);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const authenticated = authSession !== null;

  // 公开路由：登录页、重置密码页（后者是从邮件里点进来的，**不该**要求先登录）
  const isPublicRoute = pathname === '/login' || pathname.startsWith('/reset-password');

  function handleLogout() {
    // 先调接口吊销 refresh（失败也无所谓：本地一定会清，见 api/auth.logout）
    const refreshToken = getRefreshToken();
    if (refreshToken) void logoutApi(refreshToken).catch(() => undefined);
    // clearSession 会 emit → 上面那个 useSyncExternalStore 自动重渲染，
    // 不需要（也没有）setState 可以调
    clearSession();
    navigate('/login', { replace: true });
  }

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

  // ⚠ 依赖 authenticated：未登录时不发这三条请求。
  //   发了也会 401，然后被拦截器踢到登录页 —— 看似"结果一样"，实际会多出一轮
  //   无意义的请求 + 一次强制跳转，登录页会闪一下再跳，观感很差。
  useEffect(() => {
    if (!authenticated) return;
    void getHealth().then(setHealth).catch(() => setHealth(null));
    void listSessions().then(setSessions).catch(() => setSessions([]));
    refreshModels();
  }, [authenticated, refreshModels]);

  // 新会话产生后刷新侧栏「最近会话」列表（ChatPage 拿到首个 session_id 时调用）
  const refreshSessions = useCallback(() => {
    void listSessions().then(setSessions).catch(() => undefined);
  }, []);

  // 点侧栏历史会话：push 进浏览器历史 —— 之后可以用「后退」回到之前那页
  function selectSession(id: string) {
    navigate(`/chat/${id}`);
  }

  // ---- 未登录（或正在登录页/重置页）：只挂公开路由，外壳整个不渲染 ----
  // 注意这里是**早返回**而不是"渲染外壳但把内容换成登录页"：
  //   外壳会渲染侧栏的会话列表、顶栏的模型下拉 —— 那些数据都是登录后才该有的。
  if (!authenticated || isPublicRoute) {
    // 已登录的用户访问 /login：直接送回对话台（避免"登录了还停在登录页"）
    if (authenticated && pathname === '/login') return <Navigate to="/chat" replace />;
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/reset-password" element={<ResetPasswordPage />} />
        {/* 未登录访问任何其它地址 → 登录页（next 让登录后能回到原处） */}
        <Route
          path="*"
          element={<Navigate to={`/login?next=${encodeURIComponent(pathname)}`} replace />}
        />
      </Routes>
    );
  }

  return (
    <AppShell
      user={authSession.user}
      onLogout={handleLogout}
      onChangePassword={() => setPasswordOpen(true)}
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
        {/* 我的画像：所有登录用户可见（后端只认自己的画像，没有"看别人"的接口） */}
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/eval" element={<EvalPage />} />
        {/* 块 4：分析页已并入 /eval —— 旧链接重定向（replace：不在历史里留一条死路径） */}
        <Route path="/analytics" element={<Navigate to="/eval" replace />} />
        <Route path="/tools" element={<ToolsPage />} />
        <Route path="/trace" element={<TracePage />} />
        <Route path="/retriever" element={<RetrieverPage />} />
        <Route path="/admin" element={<AdminPage />} />
        {/* 未登录的路由在登录后直接命中这里（例如 /login 已被上面处理） */}
        <Route path="/login" element={<Navigate to="/chat" replace />} />
        {/* 未知路径统一回对话台 */}
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
      <ChangePasswordModal
        open={passwordOpen}
        onClose={() => setPasswordOpen(false)}
        onChanged={() => {
          // 改密成功后旧的 refresh 已作废 → 清本地并回登录页（后端也这么建议）
          setPasswordOpen(false);
          clearSession();
          navigate('/login', { replace: true });
        }}
      />
    </AppShell>
  );
}
