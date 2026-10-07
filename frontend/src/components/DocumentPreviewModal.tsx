import { FileMarkdownOutlined, FilePdfOutlined, FileTextOutlined, FileWordOutlined } from '@ant-design/icons';
import { Alert, Descriptions, Empty, Modal, Spin, Tag } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getDocumentPreview } from '../api/documents';
import { flattenPreviewChunks, getDocumentKind, type DocumentKind } from '../lib/documentPreview';
import type { DocumentOut, DocumentPreviewOut } from '../types/api';
import { formatDateTime } from '../lib/format';
import { StatusDot } from './StatusDot';
import styles from '../styles/dataPages.module.css';

function documentIcon(kind: DocumentKind) {
  if (kind === 'markdown') return <FileMarkdownOutlined />;
  if (kind === 'pdf') return <FilePdfOutlined />;
  if (kind === 'word') return <FileWordOutlined />;
  return <FileTextOutlined />;
}

function statusMeta(document: DocumentOut) {
  if (document.status === 'ready') return { tone: 'success' as const, label: '已就绪' };
  if (document.status === 'processing') return { tone: 'warning' as const, label: '处理中' };
  return { tone: 'danger' as const, label: '失败' };
}

export function DocumentPreviewModal({ document, open, onClose }: { document: DocumentOut | null; open: boolean; onClose: () => void }) {
  const [preview, setPreview] = useState<DocumentPreviewOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const kind = useMemo(() => document ? getDocumentKind(document.filename) : 'unknown', [document]);
  const content = useMemo(() => preview ? flattenPreviewChunks(preview.chunks) : '', [preview]);

  useEffect(() => {
    if (!open || !document) return;
    setPreview(null);
    setError(null);
    setLoading(true);
    void getDocumentPreview(document.id).then(setPreview).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载文档预览')).finally(() => setLoading(false));
  }, [document, open]);

  const meta = document ? statusMeta(document) : null;
  return (
    <Modal open={open} onCancel={onClose} footer={null} width={860} centered title={document ? <span className={styles.previewModalTitle}>{documentIcon(kind)} {document.filename}</span> : '文档详情'} destroyOnClose>
      {document && meta ? <div className={styles.documentPreview}>
        <Descriptions className={styles.documentMeta} size="small" bordered column={2}>
          <Descriptions.Item label="文件名">{document.filename}</Descriptions.Item>
          <Descriptions.Item label="格式"><Tag>{document.file_type.toUpperCase()}</Tag></Descriptions.Item>
          <Descriptions.Item label="状态"><StatusDot tone={meta.tone} label={meta.label} /></Descriptions.Item>
          <Descriptions.Item label="切片数"><span className="af-num">{document.chunk_count || '—'}</span></Descriptions.Item>
          <Descriptions.Item label="上传时间"><span className="af-num">{formatDateTime(document.created_at)}</span></Descriptions.Item>
          <Descriptions.Item label="文档 ID"><span className="af-mono">{document.id}</span></Descriptions.Item>
          {document.error_message ? <Descriptions.Item label="失败原因" span={2}><span className={styles.errorText}>{document.error_message}</span></Descriptions.Item> : null}
        </Descriptions>
        <div className={styles.previewHeader}><strong>文本预览</strong><span className="af-muted">{kind === 'pdf' ? 'PDF 提取文本，页码来自解析结果' : kind === 'word' ? 'DOC/DOCX 提取文本' : kind === 'markdown' ? 'Markdown 渲染' : '纯文本'}</span></div>
        {loading ? <div className={styles.previewLoading}><Spin size="small" /> 正在读取已解析内容…</div> : error ? <Alert type="error" showIcon message={error} description="请确认文档已完成处理，或稍后重试。" /> : document.status === 'processing' ? <Empty image={null} description="文档仍在处理中，完成后才能预览。" /> : !content ? <Empty image={null} description="该文档没有可展示的文本内容。" /> : <div className={`${styles.previewSurface} ${kind === 'markdown' ? styles.markdownSurface : ''}`}>{kind === 'markdown' ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown> : <pre>{content}</pre>}</div>}
      </div> : null}
    </Modal>
  );
}
