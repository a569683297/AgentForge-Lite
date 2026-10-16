import {
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
import { Button, Layout } from 'antd';
import { useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import type { HealthResponse, ModelItem, SessionItem } from '../types/api';
import type { ModelSelection } from '../lib/modelMemory';
import { formatDateTime } from '../lib/format';
import { PAGE_PATHS, type PageKey } from '../lib/routes';
import { useTheme } from '../theme/ThemeProvider';
import { ModelConfigModal } from './ModelConfigModal';
import { ModelSwitcher } from './ModelSwitcher';
import { StatusDot } from './StatusDot';
import styles from '../styles/shell.module.css';

const { Sider, Header, Content } = Layout;

const navItems: Array<{ key: PageKey; label: string; icon: ReactNode }> = [
  { key: 'chat', label: '对话台', icon: <MessageOutlined /> },
  { key: 'knowledge', label: '知识库', icon: <DatabaseOutlined /> },
  // 块 4：原「评测看板」与「分析」两页合并为一页（/eval），导航 7 → 6 项。
  // 两页本就同类（都是"看系统"的数据页），且分析页的四卡/曲线与评测页的指标卡
  // 是同一套视觉语言——分开反而把"质量"与"成本/延迟"割成两个入口。
  { key: 'eval', label: '评测与分析', icon: <ExperimentOutlined /> },
  { key: 'tools', label: '工具注册表', icon: <ToolOutlined /> },
  { key: 'trace', label: 'Trace 时间线', icon: <FundProjectionScreenOutlined /> },
  { key: 'retriever', label: '检索检查器', icon: <SearchOutlined /> },
];

export function AppShell({ page, children, sessions, health, onSessionSelect, selectedSessionId, models, selectedModelId, onModelSelect, onModelsRefresh }: {
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
}) {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  // 配置弹窗的开合状态放在这里（入口在下拉底部，两者同属顶栏交互）
  const [modelConfigOpen, setModelConfigOpen] = useState(false);
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
          {/* 翻页 = 换 URL（navigate）—— 高亮仍由 page prop 决定，但那个 prop 在 App 里
              也是从 URL 反推的，所以「地址栏 → 高亮」是一条单向链，不存在两份状态 */}
          {navItems.map((item) => (
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
