import { DeleteOutlined, FileTextOutlined, InboxOutlined, ReloadOutlined, UploadOutlined } from '@ant-design/icons';
import { Button, Card, Input, Popconfirm, Select, Space, Table, Tag, message } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { deleteDocument, listDocuments, uploadDocument } from '../api/documents';
import { DocumentPreviewModal } from '../components/DocumentPreviewModal';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { StatusDot, type StatusTone } from '../components/StatusDot';
import { TableSkeleton } from '../components/Skeleton';
import { formatDateTime } from '../lib/format';
import type { DocumentOut } from '../types/api';
import styles from '../styles/dataPages.module.css';

const statusMeta: Record<DocumentOut['status'], { tone: StatusTone; label: string }> = {
  ready: { tone: 'success', label: '已就绪' },
  processing: { tone: 'warning', label: '处理中' },
  failed: { tone: 'danger', label: '失败' },
};

export function KnowledgePage() {
  const [documents, setDocuments] = useState<DocumentOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<'all' | DocumentOut['status']>('all');
  const [selectedDocument, setSelectedDocument] = useState<DocumentOut | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const inputRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    setError(null);
    try {
      setDocuments(await listDocuments());
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '无法加载文档列表');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, []);

  useEffect(() => {
    if (!documents.some((document) => document.status === 'processing')) return undefined;
    const timer = window.setInterval(() => { void refresh(); }, 2000);
    return () => window.clearInterval(timer);
  }, [documents]);

  const filtered = useMemo(() => documents.filter((document) => (
    (status === 'all' || document.status === status) && document.filename.toLowerCase().includes(query.toLowerCase())
  )), [documents, query, status]);

  // 搜索 / 状态筛选变化时回到第 1 页：页码是在「筛选后数据集」上的位置，
  // 筛选条件一变，旧页码就失去意义（比如原在第 3 页，筛完只剩 2 页 → 空白页）。
  useEffect(() => { setPage(1); }, [query, status]);

  // 渲染时钳位：删除最后一条后当前页可能超界（page=2 但只剩 1 页）。
  // 不直接改 state，而是在渲染时取 page 与最大页的较小值 —— 无副作用且天然兜底。
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const stats = useMemo(() => ({
    total: documents.length,
    ready: documents.filter((document) => document.status === 'ready').length,
    processing: documents.filter((document) => document.status === 'processing').length,
    chunks: documents.reduce((sum, document) => sum + (document.chunk_count || 0), 0),
  }), [documents]);

  async function handleUpload(file: File) {
    try {
      const document = await uploadDocument(file);
      setDocuments((current) => [document, ...current]);
      setPage(1);                              // 新文档插在最前：跳回第 1 页，让它立刻可见
      message.success('文档已上传，正在处理');
    } catch (reason: unknown) {
      message.error(reason instanceof Error ? reason.message : '文档上传失败');
    }
  }

  async function handleDelete(id: string) {
    try {
      await deleteDocument(id);
      setDocuments((current) => current.filter((document) => document.id !== id));
      message.success('文档已删除');
    } catch (reason: unknown) {
      message.error(reason instanceof Error ? reason.message : '删除失败');
    }
  }

  const columns = [
    { title: '文件名', dataIndex: 'filename', key: 'filename', render: (value: string, record: DocumentOut) => <div className={styles.fileCell}><span className={styles.fileIcon}><FileTextOutlined /></span><div><div>{value}</div>{record.status === 'failed' && record.error_message ? <small className={styles.errorText}>{record.error_message}</small> : null}{record.status === 'processing' ? <div className={styles.progressLine}><span /></div> : null}</div></div> },
    { title: '类型', dataIndex: 'file_type', key: 'file_type', render: (value: string) => <span className="af-mono">{value.toUpperCase()}</span> },
    { title: '状态', dataIndex: 'status', key: 'status', render: (value: DocumentOut['status']) => <StatusDot tone={statusMeta[value].tone} label={statusMeta[value].label} /> },
    { title: '切片数', dataIndex: 'chunk_count', key: 'chunk_count', align: 'right' as const, render: (value: number) => <span className="af-num">{value || '—'}</span> },
    { title: '上传时间', dataIndex: 'created_at', key: 'created_at', render: (value: string) => <span className="af-num">{formatDateTime(value)}</span> },
    { title: '', key: 'action', width: 72, render: (_: unknown, record: DocumentOut) => <span onClick={(event) => event.stopPropagation()}><Popconfirm title="确认删除这份文档？" onConfirm={() => void handleDelete(record.id)}><Button type="text" danger size="small" icon={<DeleteOutlined />} aria-label={`删除 ${record.filename}`} /></Popconfirm></span> },
  ];

  return (
    <div>
      <PageHeader title="知识库" description="管理可检索文档与索引处理状态。" action={<><input ref={inputRef} type="file" hidden accept=".pdf,.docx,.md,.txt" onChange={(event) => { const file = event.target.files?.[0]; if (file) void handleUpload(file); event.target.value = ''; }} /><Button type="primary" icon={<UploadOutlined />} onClick={() => inputRef.current?.click()}>上传文档</Button></>} />
      <div className={styles.statStrip}><span><strong className="af-num">{stats.total}</strong> 份文档</span><i /><span><strong className="af-num">{stats.ready}</strong> 已就绪</span><i /><span><strong className="af-num">{stats.processing}</strong> 处理中</span><i /><span><strong className="af-num">{stats.chunks}</strong> 个切片</span></div>
      <Card className={styles.tableCard} styles={{ body: { padding: 0 } }}>
        <div className={styles.tableToolbar}><Input allowClear prefix={<InboxOutlined />} placeholder="搜索文件名" value={query} onChange={(event) => setQuery(event.target.value)} className={styles.searchBox} /><Select value={status} onChange={setStatus} options={[{ value: 'all', label: '全部状态' }, { value: 'ready', label: '已就绪' }, { value: 'processing', label: '处理中' }, { value: 'failed', label: '失败' }]} /></div>
        {loading ? <TableSkeleton rows={5} columns={6} /> : error ? <div className={styles.panelPadding}><ErrorState message={error} action="重新加载" onAction={() => void refresh()} /></div> : !filtered.length ? <EmptyState message={documents.length ? '没有匹配的文档。' : '还没有文档，上传一份开始建立知识库。'} action={documents.length ? undefined : '上传文档'} onAction={documents.length ? undefined : () => inputRef.current?.click()} /> : (
          <Table
            rowKey="id"
            size="middle"
            dataSource={filtered}
            columns={columns}
            pagination={{
              current: currentPage,
              pageSize,
              total: filtered.length,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50],
              showTotal: (total) => `共 ${total} 篇`,
              // 切换每页条数时回到第 1 页（旧页号在新粒度下没有对应位置）；仅翻页时保持所选页
              onChange: (nextPage, nextSize) => {
                setPage(nextSize === pageSize ? nextPage : 1);
                setPageSize(nextSize);
              },
            }}
            onRow={(record) => ({ onClick: () => setSelectedDocument(record), style: { cursor: 'pointer' } })}
          />
        )}
      </Card>
      <div className={styles.footnote}><ReloadOutlined /> 共 <span className="af-num">{stats.total}</span> 份文档 · 已就绪 <span className="af-num">{stats.ready}</span> · 处理中 <span className="af-num">{stats.processing}</span> · 共 <span className="af-num">{stats.chunks}</span> 个切片</div>
      <DocumentPreviewModal document={selectedDocument} open={Boolean(selectedDocument)} onClose={() => setSelectedDocument(null)} />
    </div>
  );
}
