import { EyeOutlined, PlayCircleOutlined } from '@ant-design/icons';
import { Button, Card, Modal, Select, Table, Tooltip } from 'antd';
import type { TableColumnsType } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { getEvalCaseResult, getEvalRun, listEvalRunCases, listEvalRuns } from '../api/eval';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { MetricCard } from '../components/MetricCard';
import { PageHeader } from '../components/PageHeader';
import { TableSkeleton } from '../components/Skeleton';
import { formatDateTime, formatPercent, formatScore } from '../lib/format';
import type { EvalCaseResultDetailOut, EvalCaseResultOut, EvalRunDetailOut, EvalRunOut } from '../types/api';
import styles from '../styles/dataPages.module.css';

const configLabel: Record<string, string> = { pure_vector: 'pure_vector', hybrid: 'hybrid', hybrid_rerank: 'hybrid_rerank' };

export function EvalPage() {
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number>();
  const [detail, setDetail] = useState<EvalRunDetailOut | null>(null);
  const [cases, setCases] = useState<EvalCaseResultOut[]>([]);
  const [category, setCategory] = useState<string | undefined>();
  const [passed, setPassed] = useState<boolean | undefined>();
  const [selectedCase, setSelectedCase] = useState<EvalCaseResultDetailOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void listEvalRuns().then((data) => {
      setRuns(data);
      if (data[0]) setSelectedRunId(data[0].id);
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载评测运行')).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (selectedRunId == null) return;
    setDetail(null);
    void Promise.all([getEvalRun(selectedRunId), listEvalRunCases(selectedRunId, { category, passed })]).then(([run, runCases]) => { setDetail(run); setCases(runCases); }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载评测详情'));
  }, [selectedRunId, category, passed]);

  const selectedRun = runs.find((run) => run.id === selectedRunId);
  const maxFailure = Math.max(1, ...Object.values(detail?.failure_breakdown ?? {}));
  const categories = useMemo(() => detail?.by_category ?? [], [detail]);

  async function openCase(record: EvalCaseResultOut) {
    if (selectedRunId == null) return;
    try { setSelectedCase(await getEvalCaseResult(selectedRunId, record.case_key)); } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '无法加载题目详情'); }
  }

  const caseColumns: TableColumnsType<EvalCaseResultOut> = [
    { title: '题号', dataIndex: 'case_key', width: 86, render: (value: string) => <span className="af-mono">{value}</span> },
    { title: '类别', dataIndex: 'category', width: 110 },
    { title: '答案摘要', dataIndex: 'answer', render: (value: string) => <span className={styles.truncateCell}>{value || '工具调用题'}</span> },
    { title: '引用数', dataIndex: 'sources_count', align: 'right', width: 82, render: (value: number) => <span className="af-num">{value}</span> },
    { title: '正确 / 忠实 / 完整', key: 'scores', width: 150, render: (_: unknown, row: EvalCaseResultOut) => <span className="af-num">{formatScore(row.score_correctness)} / {formatScore(row.score_faithfulness)} / {formatScore(row.score_completeness)}</span> },
    { title: '结果', dataIndex: 'passed', width: 70, render: (value: boolean) => <span className={value ? styles.passText : styles.failText}>{value ? '通过' : '失败'}</span> },
    { title: '失败原因', dataIndex: 'failure_reason_label', render: (value: string) => value || '—' },
    { title: '', key: 'action', width: 46, render: (_: unknown, row: EvalCaseResultOut) => <Button type="text" size="small" icon={<EyeOutlined />} onClick={() => void openCase(row)} aria-label={`查看 ${row.case_key}`} /> },
  ];

  return (
    <div>
      <PageHeader title="评测看板" description="比较检索配置，定位回答质量与失败模式。" badge={detail ? undefined : '等待数据'} action={<Tooltip title="评测暂由命令行触发：uv run python -m scripts.eval_run"><span><Button disabled icon={<PlayCircleOutlined />}>运行新评测</Button></span></Tooltip>} />
      {loading ? <TableSkeleton rows={5} columns={4} /> : error && !runs.length ? <ErrorState message={error} action="重新加载" onAction={() => window.location.reload()} /> : !runs.length ? <EmptyState message="还没有评测记录。评测暂由命令行触发。" /> : <>
        <div className={styles.runGrid}>{runs.slice(0, 3).map((run) => <button className={`${styles.runCard} ${run.id === selectedRunId ? styles.runCardSelected : ''}`} key={run.id} onClick={() => setSelectedRunId(run.id)}><div className={styles.runCardTop}><span className="af-mono">{configLabel[run.config_name] ?? run.config_name}</span><strong className="af-num">{formatPercent(run.accuracy)}</strong></div><div className={styles.runCardMeta}>{formatDateTime(run.created_at)} · {run.judge_model ?? '未记录 judge'}</div><div className={styles.fingerprints}><span className="af-mono">题集 {run.dataset_fingerprint?.slice(0, 8) ?? '—'}…</span><span className="af-mono">语料 {run.corpus_fingerprint?.slice(0, 8) ?? '—'}…</span></div></button>)}</div>
        <div className={styles.metricGrid}>
          <MetricCard label="准确率" value={formatPercent(detail?.accuracy)} note="按题计算，每题多次生成先取多数结论" />
          <MetricCard label="正确性均分" value={formatScore(detail?.score_correctness)} note={`按行计算，分母是 ${detail?.scored_rows ?? '—'} 行`} />
          <MetricCard label="引用忠实度" value={formatScore(detail?.score_faithfulness)} note="工具题不计内容分数" />
          <MetricCard label="完整性" value={formatScore(detail?.score_completeness)} note={`明细总行数 ${detail?.rows ?? '—'}`} />
        </div>
        <div className={styles.splitGrid}>
          <Card className={styles.panel} styles={{ body: { padding: 0 } }} title={<span>分类聚合</span>}>
            <Table size="small" pagination={false} rowKey="category" dataSource={categories} columns={[{ title: '类别', dataIndex: 'category' }, { title: '题数', dataIndex: 'cases', align: 'right', render: (value: number) => <span className="af-num">{value}</span> }, { title: '明细行数', dataIndex: 'rows', align: 'right', render: (value: number) => <span className="af-num">{value}</span> }, { title: '通过行数', dataIndex: 'passed_rows', align: 'right', render: (value: number) => <span className="af-num">{value}</span> }, { title: '正确 / 忠实 / 完整', key: 'scores', align: 'right', render: (_: unknown, row) => <span className="af-num">{formatScore(row.avg_correctness)} / {formatScore(row.avg_faithfulness)} / {formatScore(row.avg_completeness)}</span> }]} />
            <div className={styles.panelNote}>工具题只看工具调用，不送 judge 判内容，因此三维分数显示为 —。</div>
          </Card>
          <Card className={styles.panel} styles={{ body: { padding: 0 } }} title={<span>失败模式</span>}>
            <div className={styles.failureList}>{Object.entries(detail?.failure_breakdown ?? {}).map(([name, count]) => <div className={styles.failureRow} key={name}><span>{name}</span><div className={styles.failureBar}><span style={{ width: `${(count / maxFailure) * 100}%` }} /></div><span className="af-num">{count}</span></div>)}{!Object.keys(detail?.failure_breakdown ?? {}).length ? <div className={styles.panelPadding}>当前运行没有失败记录。</div> : null}</div>
          </Card>
        </div>
        <Card className={`${styles.panel} ${styles.casesPanel}`} styles={{ body: { padding: 0 } }} title={<div className={styles.filterRow}><span>逐题明细</span><Select allowClear placeholder="全部类别" value={category} onChange={setCategory} options={[{ value: 'doc_qa', label: '文档问答' }, { value: 'cross_doc', label: '跨文档' }, { value: 'tool_call', label: '工具调用' }]} /><Select allowClear placeholder="全部结果" value={passed == null ? undefined : passed ? 'passed' : 'failed'} onChange={(value) => setPassed(value == null ? undefined : value === 'passed')} options={[{ value: 'passed', label: '通过' }, { value: 'failed', label: '失败' }]} /></div>}>
          <Table size="small" pagination={{ pageSize: 15, size: 'small' }} rowKey="id" dataSource={cases} columns={caseColumns} />
        </Card>
        <Modal open={Boolean(selectedCase)} onCancel={() => setSelectedCase(null)} footer={null} title={selectedCase ? `${selectedCase.case_key} · ${selectedCase.failure_reason_label || '结果详情'}` : '结果详情'} width={720}>
          {selectedCase ? <div className={styles.caseDetail}><div className={styles.caseScore}><span>判定：{selectedCase.passed ? '通过' : '失败'}</span><span className="af-num">正确 {formatScore(selectedCase.score_correctness)} · 忠实 {formatScore(selectedCase.score_faithfulness)} · 完整 {formatScore(selectedCase.score_completeness)}</span></div><h4>答案</h4><p>{selectedCase.answer || '该题为工具调用题，没有内容答案。'}</p><h4>检索片段原文</h4><pre>{selectedCase.retrieved_chunks}</pre></div> : null}
        </Modal>
      </>}
      {selectedRun ? <div className={styles.footnote}>当前配置：<span className="af-mono">{selectedRun.config_name}</span> · 生成次数 <span className="af-num">{selectedRun.generation_runs ?? '—'}</span> · generation 不一致题数 <span className="af-num">{detail?.generation_inconsistent ?? '—'}</span></div> : null}
    </div>
  );
}
