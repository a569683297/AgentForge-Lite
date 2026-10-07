import { ApiOutlined, SettingOutlined, ToolOutlined } from '@ant-design/icons';
import { Button, Card, Input, Tag, Tooltip } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { getTools } from '../api/tools';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { StatusDot } from '../components/StatusDot';
import { TableSkeleton } from '../components/Skeleton';
import type { ToolItem, ToolsResponse } from '../types/api';
import styles from '../styles/dataPages.module.css';

export function ToolsPage() {
  const [data, setData] = useState<ToolsResponse | null>(null);
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('all');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { void getTools().then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载工具注册表')); }, []);

  const serverConnected = useMemo(() => new Map((data?.servers ?? []).map((server) => [server.name, server.connected])), [data]);
  const sources = useMemo(() => Object.entries(data?.by_source ?? {}).map(([name, names]) => ({ name, count: names.length })), [data]);
  const filtered = useMemo(() => (data?.tools ?? []).filter((tool) => {
    const matchesQuery = `${tool.name} ${tool.description}`.toLowerCase().includes(query.toLowerCase());
    return matchesQuery && (source === 'all' || tool.source === source);
  }), [data, query, source]);

  function toolStatus(tool: ToolItem) {
    const connected = tool.source === 'local' || serverConnected.get(tool.source) !== false;
    return connected ? { tone: 'success' as const, label: '可用' } : { tone: 'warning' as const, label: '未连接' };
  }

  return (
    <div>
      <PageHeader title="工具注册表" description={`已注册的 MCP 与本地工具，共 ${data?.total ?? '—'} 个。`} action={<Tooltip title="工具由 MCP server 在启动时动态发现"><span><Button disabled icon={<ApiOutlined />}>注册工具</Button></span></Tooltip>} />
      {error ? <ErrorState message={error} action="重新加载" onAction={() => window.location.reload()} /> : !data ? <TableSkeleton rows={5} columns={3} /> : !data.tools.length ? <EmptyState message="未连接 Server，当前没有可展示的工具。" /> : <>
        <div className={styles.toolFilters}><Input allowClear placeholder="搜索工具名称或描述" value={query} onChange={(event) => setQuery(event.target.value)} className={styles.searchBox} /><button className={`${styles.filterButton} ${source === 'all' ? styles.filterButtonActive : ''}`} onClick={() => setSource('all')}>全部 ({data.total})</button>{sources.map((item) => <button className={`${styles.filterButton} ${source === item.name ? styles.filterButtonActive : ''}`} onClick={() => setSource(item.name)} key={item.name}>{item.name} ({item.count})</button>)}</div>
        {!filtered.length ? <EmptyState message="没有匹配的工具。" action="清空筛选" onAction={() => { setQuery(''); setSource('all'); }} /> : <div className={styles.toolGrid}>{filtered.map((tool) => { const status = toolStatus(tool); const isOpen = expanded === tool.name; return <Card className={styles.toolCard} styles={{ body: { padding: 0 } }} key={tool.name} onClick={() => setExpanded(isOpen ? null : tool.name)}><div className={styles.toolCardHead}><span className={styles.toolIcon}><ToolOutlined /></span><StatusDot tone={status.tone} label={status.label} /></div><div className={styles.toolCardName}>{tool.name}</div><div className={styles.toolCardDescription}>{tool.description || '未提供描述'}</div><div className={styles.toolCardFoot}><Tag>{tool.source === 'local' ? '本地' : tool.source}</Tag><span>参数 Schema</span><Button type="text" size="small" icon={<SettingOutlined />} onClick={(event) => { event.stopPropagation(); setExpanded(isOpen ? null : tool.name); }} aria-label={`查看 ${tool.name} 参数`} /></div>{isOpen ? <pre className={styles.schema}>{JSON.stringify(tool.parameters, null, 2)}</pre> : null}</Card>; })}</div>}
      </>}
    </div>
  );
}
