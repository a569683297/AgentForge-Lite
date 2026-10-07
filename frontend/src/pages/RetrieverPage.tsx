import { SearchOutlined } from '@ant-design/icons';
import { Button, Input, Select } from 'antd';
import { useMemo, useState } from 'react';
import { MOCK_RETRIEVAL } from '../mock';
import { PageHeader } from '../components/PageHeader';
import { StatusDot } from '../components/StatusDot';
import styles from '../styles/dataPages.module.css';

export function RetrieverPage() {
  const [query, setQuery] = useState('为什么混合检索优于纯向量？');
  const finalRows = useMemo(() => MOCK_RETRIEVAL.filter((row) => row.rank_after != null), []);
  return (
    <div>
      <PageHeader title="检索检查器" description="并排检查候选池、RRF 融合和重排后的取舍过程。" badge="示例数据" action={<Button type="primary" icon={<SearchOutlined />}>检索</Button>} />
      <div className={styles.traceToolbar}><Input value={query} onChange={(event) => setQuery(event.target.value)} prefix={<SearchOutlined />} /><Select value={5} options={[{ value: 5, label: 'top-k = 5' }, { value: 10, label: 'top-k = 10' }]} style={{ width: 120 }} /></div>
      <div className={styles.rankGrid}>
        <section className={styles.rankColumn}><div className={styles.rankHeader}>重排前 · 候选池 20</div>{MOCK_RETRIEVAL.map((row) => <div className={`${styles.rankItem} ${row.is_gold ? styles.rankItemGold : ''}`} key={`${row.doc}-${row.rank_before}`}><span className="af-num">#{row.rank_before}</span><span className="af-truncate">{row.doc}</span><span className={`${styles.rankScore} af-num`}>{row.score_logit.toFixed(3)}</span><StatusDot tone={row.is_gold ? 'success' : 'muted'} /></div>)}</section>
        <section className={styles.rankColumn}><div className={styles.rankHeader}>重排后 · 最终 top-{finalRows.length}</div>{finalRows.map((row) => <div className={`${styles.rankItem} ${row.is_gold ? styles.rankItemGold : ''}`} key={`${row.doc}-${row.rank_after}`}><span className="af-num">#{row.rank_after}</span><span className="af-truncate">{row.doc}</span><span className={`${styles.rankScore} af-num`}>{row.score_logit.toFixed(3)}</span><StatusDot tone={row.is_gold ? 'success' : 'muted'} /></div>)}</section>
      </div>
      <div className={styles.retrieverNote}>分数单位是 reranker logit，不是概率；绿色色点表示命中 gold 证据，不能把整行理解为“正确/错误”色块。</div>
    </div>
  );
}
