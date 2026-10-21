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
import type { AuthUser } from '../lib/auth';
import type { ModelSelection } from '../lib/modelMemory';
import { formatDateTime } from '../lib/format';
import { PAGE_PATHS, type PageKey } from '../lib/routes';
import { useTheme } from '../theme/ThemeProvider';
import { ModelConfigModal } from './ModelConfigModal';
import { ModelSwitcher } from './ModelSwitcher';
import { StatusDot } from './StatusDot';
import styles from '../styles/shell.module.css';

const { Sider, Header, Content } = Layout;

/**
 * 导航项。`adminOnly` 决定"普通用户看不看得见"。
 *
 * ⚠ 隐藏入口 ≠ 有权限：后端对这些路径都挂了 `require_admin`（403）。
 *   前端这份清单只是"不把走不通的路摆出来"，任何越权尝试仍由服务端拒绝 ——
 *   把两者搞混（以为前端藏起来就安全）是权限设计里最常见的错误。
 */
const navItems: Array<{ key: PageKey; label: string; icon: ReactNode; adminOnly?: boolean }> = [
  { key: 'chat', label: '对话台', icon: <MessageOutlined /> },
  // 我的画像：**不是管理员页**。它决定 Agent 怎么跟你说话（称呼、职业、回答风格），
  // 每个登录用户都该有，且紧挨对话台 —— 放在"设置"里会让人找不到，
  // 而找不到的画像 = 用户没法纠正错误画像 = 这个功能反而有害。
  { key: 'profile', label: '我的画像', icon: <IdcardOutlined /> },
  { key: 'knowledge', label: '知识库', icon: <DatabaseOutlined /> },
  // 块 4：原「评测看板」与「分析」两页合并为一页（/eval），导航 7 → 6 项。
  // 两页本就同类（都是"看系统"的数据页），且分析页的四卡/曲线与评测页的指标卡
  // 是同一套视觉语言——分开反而把"质量"与"成本/延迟"割成两个入口。
  // 账号体系之后：评测跑的是**平台级语料**，普通用户既没有语料管理权也不该看到全量分数，
  // 所以整页收成管理员可见（后端同步 403）。
  { key: 'eval', label: '评测与分析', icon: <ExperimentOutlined />, adminOnly: true },
  { key: 'tools', label: '工具注册表', icon: <ToolOutlined />, adminOnly: true },
  { key: 'trace', label: 'Trace 时间线', icon: <FundProjectionScreenOutlined />, adminOnly: true },
  { key: 'retriever', label: '检索检查器', icon: <SearchOutlined /> },
  { key: 'admin', label: '用户与审计', icon: <TeamOutlined />, adminOnly: true },
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
  // 普通用户看不到管理页；管理员看到全部（顺序与 navItems 声明一致）
  const visibleNav = navItems.filter((item) => !item.adminOnly || isAdmin);

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
