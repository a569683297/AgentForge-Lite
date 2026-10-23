import {
  BulbOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  MoonOutlined,
  SearchOutlined,
  SunOutlined,
  TeamOutlined,
  ToolOutlined,
  FundProjectionScreenOutlined,
  IdcardOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Layout, Tag } from 'antd';
import { useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import type { HealthResponse, ModelItem, SessionItem } from '../types/api';
import { hasPermissionOf, type AuthUser } from '../lib/auth';
import type { ModelSelection } from '../lib/modelMemory';
import { formatDateTime } from '../lib/format';
import { PAGE_PATHS, PAGE_PERMISSIONS, type PageKey } from '../lib/routes';
import { useTheme } from '../theme/ThemeProvider';
import { ModelConfigModal } from './ModelConfigModal';
import { ModelSwitcher } from './ModelSwitcher';
import { StatusDot } from './StatusDot';
import styles from '../styles/shell.module.css';

const { Sider, Header, Content } = Layout;

/**
 * 导航项。
 *
 * **这里不再写"谁能看"** —— 每个页面需要什么权限点由 `PAGE_PERMISSIONS` 声明，
 * 而"当前这个人有没有"由后端下发的 `permissions` 决定。两端各管一件事：
 *   · 页面 ↔ 权限点的映射 = 前端的知识（它知道自己有哪些页面）
 *   · 人 ↔ 权限点的映射   = 后端的知识（role → permission，只在服务端维护）
 *
 * 为什么不在这里写 `adminOnly: true`（改造前的做法）：
 *   那等于前端也维护了一份"role → 能看什么"的映射。只有两个角色时它看起来没问题，
 *   但一旦出现第三种角色（比如只读运营），前端和后端会各自演化，最后对不上 ——
 *   表现是"菜单藏着但地址能进"或反之，且不会报错。
 *
 * ⚠ 隐藏入口 ≠ 有权限：后端对每个接口都声明了权限点（403）。
 *   前端这份过滤只是"不把走不通的路摆出来"，任何越权尝试仍由服务端拒绝。
 */
const navItems: Array<{ key: PageKey; label: string; icon: ReactNode }> = [
  { key: 'chat', label: '对话台', icon: <MessageOutlined /> },
  // 我的画像：**不是管理员页**。它决定 Agent 怎么跟你说话（称呼、职业、回答风格），
  // 每个登录用户都该有，且紧挨对话台 —— 放在"设置"里会让人找不到，
  // 而找不到的画像 = 用户没法纠正错误画像 = 这个功能反而有害。
  { key: 'profile', label: '我的画像', icon: <IdcardOutlined /> },
  { key: 'knowledge', label: '知识库', icon: <DatabaseOutlined /> },
  // 块 4：原「评测看板」与「分析」两页合并为一页（/eval），导航 7 → 6 项。
  { key: 'eval', label: '评测与分析', icon: <ExperimentOutlined /> },
  { key: 'tools', label: '工具注册表', icon: <ToolOutlined /> },
  { key: 'trace', label: 'Trace 时间线', icon: <FundProjectionScreenOutlined /> },
  { key: 'retriever', label: '检索检查器', icon: <SearchOutlined /> },
  { key: 'admin', label: '用户与审计', icon: <TeamOutlined /> },
];

export function AppShell({ page, children, sessions, health, onSessionSelect, selectedSessionId, models, selectedModelId, onModelSelect, onModelsRefresh, user, onLogout, onChangePassword }: {
  page: PageKey;
  children: ReactNode;
  sessions: SessionItem[];
  health: HealthResponse | null;
  onSessionSelect: (id: string) => void;
  selectedSessionId?: string;
  models: ModelItem[];
  selectedModelId: ModelSelection;
  onModelSelect: (modelId: ModelSelection) => void;
  onModelsRefresh: () => void;
  user: AuthUser;
  onLogout: () => void;
  onChangePassword: () => void;
}) {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  // 配置弹窗的开合状态放在这里（入口在下拉底部，两者同属顶栏交互）
  const [modelConfigOpen, setModelConfigOpen] = useState(false);
  const { mode, toggle } = useTheme();
  const status = health?.status === 'ok' ? { tone: 'success' as const, label: 'API 正常' } : health?.status === 'degraded' ? { tone: 'warning' as const, label: '依赖异常' } : { tone: 'danger' as const, label: '连接失败' };
  const activeSession = useMemo(() => selectedSessionId ?? sessions[0]?.id, [selectedSessionId, sessions]);
  const isAdmin = user.role === 'admin';
  // 按后端下发的权限清单过滤：普通用户看不到平台级页面，管理员看到全部。
  // `PAGE_PERMISSIONS` 是"页面要什么"的唯一出处（与路由守卫共用同一张表）。
  const visibleNav = navItems.filter((item) =>
    hasPermissionOf(user, PAGE_PERMISSIONS[item.key]),
  );

  return (
    <Layout className={styles.appShell}>
      <Sider width={216} collapsedWidth={56} collapsible collapsed={collapsed} trigger={null} className={styles.sidebar}>
        <div className={styles.brandRow}>
          <div className={styles.brandMark}><BulbOutlined /></div>
          {!collapsed ? <strong>AgentForge</strong> : null}
          <Button type="text" size="small" aria-label={collapsed ? '展开侧栏' : '折叠侧栏'} icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed((current) => !current)} />
        </div>
        {!collapsed ? <div className={styles.navLabel}>工作台</div> : null}
        <nav className={styles.mainNav}>
          {/* 翻页 = 换 URL（navigate）—— 高亮仍由 page prop 决定，但那个 prop 在 App 里
              也是从 URL 反推的，所以「地址栏 → 高亮」是一条单向链，不存在两份状态 */}
          {visibleNav.map((item) => (
            <button className={`${styles.navItem} ${page === item.key ? styles.navActive : ''}`} onClick={() => navigate(PAGE_PATHS[item.key])} key={item.key} title={collapsed ? item.label : undefined}>
              {item.icon}<span>{collapsed ? null : item.label}</span>
            </button>
          ))}
        </nav>
        {!collapsed ? (
          <div className={styles.history}>
            <div className={styles.historyHeader}>最近会话</div>
            {sessions.slice(0, 6).map((session) => <button className={`${styles.session} ${session.id === activeSession ? styles.sessionActive : ''}`} onClick={() => onSessionSelect(session.id)} key={session.id}><span className="af-truncate">{session.title}</span><small>{formatDateTime(session.updated_at)}</small></button>)}
            {!sessions.length ? <span className={styles.historyEmpty}>还没有会话</span> : null}
          </div>
        ) : null}
        <div className={styles.sidebarBottom}><span className={styles.environmentBadge}>local</span></div>
      </Sider>
      <Layout>
        <Header className={styles.topbar}>
          <div className={styles.topbarRight}>
            <StatusDot tone={status.tone} label={status.label} />
            <ModelSwitcher
              models={models}
              selectedModelId={selectedModelId}
              onSelect={onModelSelect}
              onOpenConfig={() => setModelConfigOpen(true)}
            />
            <Button type="text" aria-label="切换主题" icon={mode === 'dark' ? <SunOutlined /> : <MoonOutlined />} onClick={toggle} />
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  { key: 'who', label: <span className={styles.userEmail}>{user.email}</span>, disabled: true },
                  { type: 'divider' },
                  { key: 'profile', label: '我的画像', icon: <IdcardOutlined /> },
                  { key: 'password', label: '修改密码' },
                  { key: 'logout', label: '退出登录', icon: <LogoutOutlined />, danger: true },
                ],
                onClick: ({ key }) => {
                  if (key === 'logout') onLogout();
                  if (key === 'password') onChangePassword();
                  if (key === 'profile') navigate(PAGE_PATHS.profile);
                },
              }}
            >
              <Button type="text" className={styles.userButton}>
                <UserOutlined />
                <span className="af-truncate">{user.display_name || user.email}</span>
                {isAdmin ? <Tag color="blue" bordered={false}>管理员</Tag> : null}
              </Button>
            </Dropdown>
          </div>
        </Header>
        <Content className={styles.pageContent}>{children}</Content>
      </Layout>
      <ModelConfigModal
        open={modelConfigOpen}
        models={models}
        onClose={() => setModelConfigOpen(false)}
        onModelsChanged={onModelsRefresh}
      />
    </Layout>
  );
}
