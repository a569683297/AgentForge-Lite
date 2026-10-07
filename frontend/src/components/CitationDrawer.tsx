import { FileTextOutlined } from '@ant-design/icons';
import { Button, Drawer, Tag } from 'antd';
import type { SourceItem } from '../types/api';
import styles from '../styles/chat.module.css';

export function CitationDrawer({ source, onClose, onOpenKnowledge }: { source: SourceItem | null; onClose: () => void; onOpenKnowledge: () => void }) {
  return (
    <Drawer open={Boolean(source)} onClose={onClose} placement="right" width={420} title={source ? `引用 [${source.index}]` : '检索来源'}>
      {source ? (
        <div className={styles.citationContent}>
          <div className={styles.sourceTitle}><FileTextOutlined /> <strong>{source.source}</strong><Tag color="success">知识库 · 已就绪</Tag></div>
          {source.page_ref ? <div className={styles.sourceMeta}>页码 <span className="af-num">{source.page_ref}</span></div> : null}
          {source.similarity != null ? <div className={styles.similarity}><span>余弦相似度</span><strong className="af-num">{source.similarity.toFixed(2)}</strong></div> : null}
          <div className={styles.sourceCopy}>{source.content}</div>
          <Button block onClick={onOpenKnowledge}>在知识库中打开 ↗</Button>
        </div>
      ) : null}
    </Drawer>
  );
}
