import { EyeOutlined, InfoCircleOutlined, PlayCircleOutlined } from '@ant-design/icons';
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

interface InfoTip { title: string; steps: string[]; note: string }

// 三种检索配置的完整链路（与 app/services/retrieval_service.py 的 retrieve() 一一对应）。
// 挂在运行卡片的 info 图标上：悬浮即解释"这一轮用的检索方案具体怎么跑"。
const retrievalFlows: Record<string, InfoTip> = {
  pure_vector: {
    title: 'pure_vector · 纯向量单路',
    steps: [
      '问题经嵌入模型转成向量',
      'pgvector 按余弦距离排序「已就绪」文档的切片',
      '取语义最近的 5 条返回',
    ],
    note: '无融合、无重排；最快，但对精确字面（型号、数字）不敏感。',
  },
  hybrid: {
    title: 'hybrid · 向量 + BM25 融合',
    steps: [
      '两路并发召回：向量 20 条 + BM25（关键词）20 条',
      'RRF 融合：丢掉分数，只按两路名次合并成一张榜单',
      '取融合后前 5 条返回',
    ],
    note: '无重排：融合名次即最终名次。',
  },
  hybrid_rerank: {
    title: 'hybrid_rerank · 融合 + 重排',
    steps: [
      '两路并发召回各 20 条 → RRF 融合保留 20 条候选',
      '重排器逐条精读「问题 + 片段」配对打分',
      '按重排分取前 5 条返回',
    ],
    note: '最慢：重排是大头（20 条候选约 750ms，D18 实测）。',
  },
};

// 两枚指纹的说明（挂在卡片底部各指纹值后的 info 图标上）——「封条」是项目里一贯的比喻。
const fingerprintTips: Record<'dataset' | 'corpus', InfoTip> = {
  dataset: {
    title: '题集指纹 · 考卷的「封条」',
    steps: [
      '把全部评测题（题面 / 参考答案 / 依据 / 类别等字段）算成 sha256 哈希，取前 8 位显示',
      '任一字段被改动（哪怕只删一道题）→ 指纹立刻变化',
      '作用：证明各配置用的是同一份题——指纹相同，分数才可横向比较',
    ],
    note: '出报告前三配置指纹必须逐字一致，不一致会被直接拒绝；依据文档列表做过排序归一（A+B 与 B+A 视为同一份）。',
  },
  corpus: {
    title: '语料指纹 · 教材的「封条」',
    steps: [
      '把知识库全部切片的内容算成 sha256 哈希，取前 8 位显示',
      '正文改动、或改了切片参数重建语料 → 指纹变化；仅重建换 UUID → 不变',
      '作用：证明各配置用的是同一份语料——分数差异才能归因到检索配置',
    ],
    note: '按全库计算：跑回归留下的残留切片也会让指纹变化，防止三个配置在不同状态的库上比较。',
  },
};

function InfoTipContent({ tip }: { tip: InfoTip }) {
  return (
    <div className={styles.flowTip}>
      <div className={styles.flowTipTitle}>{tip.title}</div>
      <div className={styles.flowTipSteps}>
        {tip.steps.map((step, index) => (
          <div className={styles.flowTipStep} key={step}>
            <span className={styles.flowTipIndex}>{index + 1}</span>
            <span>{step}</span>
          </div>
        ))}
      </div>
      <div className={styles.flowTipNote}>{tip.note}</div>
    </div>
  );
}

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
        <div className={styles.runGrid}>{runs.slice(0, 3).map((run) => {
          const flow = retrievalFlows[run.config_name];
          return <button className={`${styles.runCard} ${run.id === selectedRunId ? styles.runCardSelected : ''}`} key={run.id} onClick={() => setSelectedRunId(run.id)}><div className={styles.runCardTop}><span className={`af-mono ${styles.runCardName}`}>{configLabel[run.config_name] ?? run.config_name}{flow ? <Tooltip title={<InfoTipContent tip={flow} />}><span className={styles.runCardInfo}><InfoCircleOutlined /></span></Tooltip> : null}</span><strong className="af-num">{formatPercent(run.accuracy)}</strong></div><div className={styles.runCardMeta}>{formatDateTime(run.created_at)} · {run.judge_model ?? '未记录 judge'}</div><div className={styles.fingerprints}><span className={`af-mono ${styles.fingerprintItem}`}>题集 {run.dataset_fingerprint?.slice(0, 8) ?? '—'}…<Tooltip title={<InfoTipContent tip={fingerprintTips.dataset} />}><span className={styles.runCardInfo}><InfoCircleOutlined /></span></Tooltip></span><span className={`af-mono ${styles.fingerprintItem}`}>语料 {run.corpus_fingerprint?.slice(0, 8) ?? '—'}…<Tooltip title={<InfoTipContent tip={fingerprintTips.corpus} />}><span className={styles.runCardInfo}><InfoCircleOutlined /></span></Tooltip></span></div></button>;})}</div>
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
