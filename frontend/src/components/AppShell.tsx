import {
  BarChartOutlined,
  BulbOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  MoonOutlined,
  SearchOutlined,
  SunOutlined,
  ToolOutlined,
  FundProjectionScreenOutlined,
} from '@ant-design/icons';
import { Button, Layout, Select, Tooltip } from 'antd';
import { useMemo, useState, type ReactNode } from 'react';
import type { HealthResponse, SessionItem } from '../types/api';
import { formatDateTime } from '../lib/format';
import { useTheme } from '../theme/ThemeProvider';
import { StatusDot } from './StatusDot';
import type { PageKey } from '../App';
import styles from '../styles/shell.module.css';

const { Sider, Header, Content } = Layout;

const navItems: Array<{ key: PageKey; label: string; icon: ReactNode }> = [
  { key: 'chat', label: '对话台', icon: <MessageOutlined /> },
  { key: 'knowledge', label: '知识库', icon: <DatabaseOutlined /> },
  { key: 'eval', label: '评测看板', icon: <ExperimentOutlined /> },
  { key: 'analytics', label: '分析', icon: <BarChartOutlined /> },
  { key: 'tools', label: '工具注册表', icon: <ToolOutlined /> },
  { key: 'trace', label: 'Trace 时间线', icon: <FundProjectionScreenOutlined /> },
  { key: 'retriever', label: '检索检查器', icon: <SearchOutlined /> },
];

const titles = Object.fromEntries(navItems.map((item) => [item.key, item.label])) as Record<PageKey, string>;

export function AppShell({ page, onPageChange, children, sessions, health, onSessionSelect, selectedSessionId }: {
  page: PageKey;
  onPageChange: (page: PageKey) => void;
  children: ReactNode;
  sessions: SessionItem[];
  health: HealthResponse | null;
  onSessionSelect: (id: string) => void;
  selectedSessionId?: string;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const { mode, toggle } = useTheme();
  const status = health?.status === 'ok' ? { tone: 'success' as const, label: 'API 正常' } : health?.status === 'degraded' ? { tone: 'warning' as const, label: '依赖异常' } : { tone: 'danger' as const, label: '连接失败' };
  const activeSession = useMemo(() => selectedSessionId ?? sessions[0]?.id, [selectedSessionId, sessions]);

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
          {navItems.map((item) => (
            <button className={`${styles.navItem} ${page === item.key ? styles.navActive : ''}`} onClick={() => onPageChange(item.key)} key={item.key} title={collapsed ? item.label : undefined}>
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
          <div className={styles.topbarLeft}><span className={styles.topbarTitle}>{titles[page]}</span><span className={styles.topbarSlash}>/</span><span className="af-mono af-muted">production</span></div>
          <div className={styles.topbarRight}>
            <StatusDot tone={status.tone} label={status.label} />
            <Tooltip title="模型由后端环境变量决定，当前仅展示"><Select size="small" value="configured runtime" options={[{ value: 'configured runtime', label: 'configured runtime' }]} className={styles.modelSelect} /></Tooltip>
            <Button type="text" aria-label="切换主题" icon={mode === 'dark' ? <SunOutlined /> : <MoonOutlined />} onClick={toggle} />
          </div>
        </Header>
        <Content className={styles.pageContent}>{children}</Content>
      </Layout>
    </Layout>
  );
}
