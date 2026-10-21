import { SearchOutlined } from '@ant-design/icons';
import { Button, Input, Select, Spin } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { listEvalCases } from '../api/eval';
import { getRetrieverOptions, searchRetriever } from '../api/retriever';
import { EmptyState, ErrorState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { StatusDot } from '../components/StatusDot';
import styles from '../styles/dataPages.module.css';
import type {
  EvalCaseOut,
  RetrieverCandidate,
  RetrieverOptions,
  RetrieverSearchResponse,
} from '../types/api';

/** 三种检索配置的中文名。配置清单本身来自后端，这里只负责翻译。 */
const CONFIG_LABEL: Record<string, string> = {
  pure_vector: '纯向量',
  hybrid: '混合（RRF）',
  hybrid_rerank: '混合 + 重排',
};

/**
 * top-k 的可选档位。上限由后端给（= 候选池宽度），档位本身是纯展示选择，
 * 不需要后端下发 —— 下发反而会让"能选几项"变成另一个要同步的地方。
 */
const TOP_K_CHOICES = [1, 3, 5, 10, 20];

/** 无 gold 时的原因文案（负例与工具题按定义没有依据文档）。 */
const NO_GOLD_REASON: Record<string, string> = {
  negative: '负例：正确答案是「知识库里没有」，按定义无依据文档',
  tool_call: '工具题：判定依据是「有没有调对工具」，与检索质量无关',
};

function fmtScore(value: number | null): string {
  if (value === null || value === undefined) return '—';
  // RRF 分在 0.016~0.033，用 4 位小数才看得出差别；其余三种用 3 位就够
  return Math.abs(value) >= 1 ? value.toFixed(3) : value.toFixed(4);
}

function fmtMs(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(2)}s` : `${Math.round(value)}ms`;
}

/**
 * 名次变化：正数=上升，负数=下降。
 *
 * 这一格是整页的核心。没有它，"被挤到第 12 名"和"根本没进前 5"
 * 在界面上是同一件事（都只是"右列里没有"），而调参时想知道的恰恰是前者。
 */
function Delta({ pool, rerank }: { pool: number; rerank: number | null }) {
  if (rerank === null) return <span className={styles.retrieverFlat}>—</span>;
  const diff = pool - rerank;
  if (diff === 0) return <span className={styles.retrieverFlat}>=</span>;
  return diff > 0 ? (
    <span className={styles.retrieverUp}>↑{diff}</span>
  ) : (
    <span className={styles.retrieverDown}>↓{Math.abs(diff)}</span>
  );
}

export function RetrieverPage() {
  const [options, setOptions] = useState<RetrieverOptions | null>(null);
  const [cases, setCases] = useState<EvalCaseOut[]>([]);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  const [query, setQuery] = useState('员工累计工作满 1 年但不满 10 年的，年假有几天？');
  const [topK, setTopK] = useState(5);
  const [config, setConfig] = useState<string | undefined>(undefined);
  const [caseKey, setCaseKey] = useState<string | null>(null);

  const [result, setResult] = useState<RetrieverSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeChunk, setActiveChunk] = useState<string | null>(null);

  // 初始化：配置清单 / k 上限来自后端，题目列表复用评测接口 —— 都不在前端写死
  useEffect(() => {
    let alive = true;
    Promise.all([getRetrieverOptions(), listEvalCases()])
      .then(([opts, list]) => {
        if (!alive) return;
        setOptions(opts);
        setCases(list);
        setTopK(opts.default_top_k);
        setConfig(opts.default_config);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setOptionsError(err instanceof Error ? err.message : '初始化失败：无法读取可选项');
      });
    return () => {
      alive = false;
    };
  }, []);

  const runSearch = useCallback(async () => {
    const trimmed = query.trim();
    if (!trimmed) {
      setError('请先输入要检索的问题');
      return;
    }
    setLoading(true);
    setError(null);
    setActiveChunk(null);
    try {
      const data = await searchRetriever({
        query: trimmed,
        top_k: topK,
        config,
        case_key: caseKey,
      });
      setResult(data);
    } catch (err: unknown) {
      setResult(null);
      setError(err instanceof Error ? err.message : '检索失败');
    } finally {
      setLoading(false);
    }
  }, [query, topK, config, caseKey]);

  const finalRows = useMemo(
    () =>
      (result?.candidates ?? [])
        .filter((row) => row.final_rank !== null)
        .sort((a, b) => (a.final_rank ?? 0) - (b.final_rank ?? 0)),
    [result],
  );

  // 左列显示的是 `score` 字段（候选池阶段的分数），它的口径取决于**是否走了融合**：
  // 纯向量是余弦，其余是 RRF。不能直接用 score_kind —— 那个说的是"主分数"，
  // 在 hybrid_rerank 下指的是右列的 logit，拿来标左列会把 RRF 分说成 logit。
  const poolScoreLabel = result?.effective_config === 'pure_vector' ? '余弦相似度' : 'RRF 融合分';

  const active = useMemo(
    () => result?.candidates.find((row) => row.chunk_id === activeChunk) ?? null,
    [result, activeChunk],
  );

  const caseOptions = useMemo(
    () => [
      { value: '', label: '不选题目（自由查询，无 gold 判定）' },
      ...cases.map((item) => ({
        value: item.case_key,
        label: `${item.case_key} · ${item.question}`,
      })),
    ],
    [cases],
  );

  return (
    <div>
      <PageHeader
        title="检索检查器"
        description="看一次检索怎么从 20 条候选里挑出 5 条：融合排序、重排名次变化、以及有没有命中标准答案。"
        action={
          <Button type="primary" icon={<SearchOutlined />} loading={loading} onClick={runSearch}>
            检索
          </Button>
        }
      />

      <div className={styles.traceToolbar}>
        <Select
          showSearch
          allowClear
          style={{ width: 320 }}
          placeholder="选一道评测题（可选）"
          value={caseKey ?? ''}
          options={caseOptions}
          optionFilterProp="label"
          onChange={(value: string) => {
            const next = value || null;
            setCaseKey(next);
            // 选了题就把题面填进查询框 —— 这页最常见的用法是"复现某道题为什么没命中"
            const picked = cases.find((item) => item.case_key === next);
            if (picked) setQuery(picked.question);
          }}
        />
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onPressEnter={runSearch}
          placeholder="输入要检索的问题"
          allowClear
        />
        <Select
          style={{ width: 116 }}
          value={topK}
          onChange={(value: number) => setTopK(value)}
          options={TOP_K_CHOICES.filter((value) => value <= (options?.max_top_k ?? 20)).map(
            (value) => ({ value, label: `top-${value}` }),
          )}
        />
        <Select
          style={{ width: 150 }}
          value={config}
          onChange={(value: string) => setConfig(value)}
          options={(options?.configs ?? []).map((value) => ({
            value,
            label: CONFIG_LABEL[value] ?? value,
          }))}
        />
      </div>

      {optionsError ? (
        <ErrorState message={`${optionsError}（页面仍可手动检索，配置与 k 上限用内置默认）`} />
      ) : null}

      {loading ? (
        <div className={styles.sectionLoading}>
          <Spin size="small" /> 正在跑检索（含向量化与重排，约 1~2 秒）
        </div>
      ) : null}

      {error ? <ErrorState message={error} action="重试" onAction={runSearch} /> : null}

      {!loading && !error && !result ? (
        <EmptyState message="输入问题后点「检索」。选了评测题会自动带上它的题面，并在结果里给出 gold 命中判定。" />
      ) : null}

      {result ? (
        <>
          <div className={styles.statStrip}>
            <span>
              <strong className="af-num">{result.candidates.length}</strong> 条候选
            </span>
            <i />
            <span>
              <strong className="af-num">{finalRows.length}</strong> 条入最终 top-{result.top_k}
            </span>
            <i />
            <span>
              实际路径 <strong>{CONFIG_LABEL[result.effective_config] ?? result.effective_config}</strong>
            </span>
            <i />
            <span>
              检索 <strong className="af-num">{fmtMs(result.timings.retrieve_ms)}</strong>
            </span>
            <i />
            <span>
              重排 <strong className="af-num">{fmtMs(result.timings.rerank_ms)}</strong>
            </span>
            <i />
            <span>
              总 <strong className="af-num">{fmtMs(result.timings.total_ms)}</strong>
            </span>
          </div>

          {/* 降级：名义配置与实际路径不一致时必须说出来，否则会被读成"重排没效果" */}
          {result.degrade_reason ? (
            <div className={styles.retrieverSplit}>
              <StatusDot tone="warning" />
              {result.degrade_reason}
            </div>
          ) : null}

          {/* gold 判定三态：未选题 / 该题无依据文档 / 有判定结果 */}
          {result.gold ? (
            result.gold.has_gold ? (
              <div className={styles.retrieverSplit}>
                <StatusDot tone={result.gold.hit ? 'success' : 'danger'} />
                <span>
                  {result.gold.case_key} · 命中 {result.gold.hit_count}/{result.gold.expected_count}{' '}
                  篇
                  {result.gold.hit_rank !== null ? `，最早第 ${result.gold.hit_rank} 名` : ''}
                  ，MRR <span className="af-num">{result.gold.mrr.toFixed(2)}</span>
                </span>
                <span className={styles.retrieverRight}>
                  期望文档：{result.gold.doc_slugs.join('、') || '—'}
                </span>
              </div>
            ) : (
              <div className={styles.retrieverSplit}>
                <StatusDot tone="muted" />
                {result.gold.case_key} 无依据文档 ——{' '}
                {NO_GOLD_REASON[result.gold.is_negative ? 'negative' : result.gold.category] ??
                  '该题不参与 Hit@k 统计'}
              </div>
            )
          ) : (
            <div className={styles.retrieverSplit}>
              <StatusDot tone="muted" />
              未选评测题：本次没有 gold，色点无法判定命中（自由查询本来就没有"正确答案文档"）
            </div>
          )}

          <div className={styles.rankGrid}>
            <section className={styles.rankColumn}>
              <div className={styles.rankHeader}>
                候选池 {result.candidates.length} · {poolScoreLabel}
                <span className={styles.retrieverFormula}>（箭头 = 重排后名次变化）</span>
              </div>
              {result.candidates.map((row) => (
                <RetrieverRow
                  key={`pool-${row.chunk_id}`}
                  row={row}
                  mode="pool"
                  active={row.chunk_id === activeChunk}
                  onSelect={() => setActiveChunk(row.chunk_id)}
                />
              ))}
            </section>

            <section className={styles.rankColumn}>
              <div className={styles.rankHeader}>
                最终 top-{result.top_k} ·{' '}
                {result.reranked ? '重排后（logit）' : '无重排（沿用候选池顺序）'}
              </div>
              {finalRows.length === 0 ? (
                <div className={styles.panelInlineEmpty}>没有结果</div>
              ) : (
                finalRows.map((row) => (
                  <RetrieverRow
                    key={`final-${row.chunk_id}`}
                    row={row}
                    mode="final"
                    active={row.chunk_id === activeChunk}
                    onSelect={() => setActiveChunk(row.chunk_id)}
                  />
                ))
              )}
            </section>
          </div>

          {active ? <Excerpt row={active} reranked={result.reranked} /> : null}

          <div className={styles.retrieverNote}>
            四种分数不是同一个量纲，不能横向比较：余弦相似度 ∈ [0,1]、BM25 无上界、
            RRF 分是 1/(60+名次) 累加（约 0.016~0.033）、logit 跨查询也不可比。
            点任意一行可看该片段的正文与它在两路检索里的名次。
          </div>
        </>
      ) : null}
    </div>
  );
}

/** 一行。左右两列共用，`mode` 只决定名次与分数取哪一组。 */
function RetrieverRow({
  row,
  mode,
  active,
  onSelect,
}: {
  row: RetrieverCandidate;
  mode: 'pool' | 'final';
  active: boolean;
  onSelect: () => void;
}) {
  const className = [
    styles.retrieverRow,
    row.is_gold ? styles.retrieverRowGold : '',
    active ? styles.retrieverRowActive : '',
    // 左列里没进最终结果的降到半透明：一眼看出"这 15 条被取舍掉了"
    mode === 'pool' && row.final_rank === null ? styles.retrieverRowOut : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={className} onClick={onSelect} role="button" tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onSelect();
      }}
    >
      <span className={`${styles.retrieverRank} af-num`}>
        #{mode === 'pool' ? row.pool_rank : row.final_rank}
      </span>
      <span className={styles.retrieverDoc} title={row.source ?? ''}>
        {row.source ?? '—'}
      </span>
      <span className={`${styles.rankScore} af-num`}>
        {fmtScore(mode === 'final' ? row.rerank_score ?? row.score : row.score)}
      </span>
      <span className={styles.retrieverDelta}>
        {mode === 'pool' ? (
          <Delta pool={row.pool_rank} rerank={row.rerank_rank} />
        ) : (
          <span className={styles.retrieverFlat}>池 #{row.pool_rank}</span>
        )}
      </span>
      {/* 没有 gold 时不画点：全灰会被读成"都没命中"，而真相是"没在判" */}
      {row.is_gold === null ? <span /> : <StatusDot tone={row.is_gold ? 'success' : 'muted'} />}
    </div>
  );
}

/** 选中片段的正文 + 它在两路检索里的位置（调参真正要看的东西）。 */
function Excerpt({ row, reranked }: { row: RetrieverCandidate; reranked: boolean }) {
  return (
    <div className={styles.retrieverExcerpt}>
      <div className={styles.retrieverExcerptHead}>
        <strong>池 #{row.pool_rank}</strong>
        <span>{row.source ?? '—'}</span>
        {row.page_ref ? <span>第 {row.page_ref} 页</span> : null}
        <span className={styles.retrieverFlat}>切片 {row.chunk_id.slice(0, 8)}</span>
        <span className={`${styles.retrieverRight} ${styles.retrieverFlat}`}>
          向量 {row.vector_rank === null ? '未命中' : `#${row.vector_rank}`} · BM25{' '}
          {row.bm25_rank === null ? '未命中' : `#${row.bm25_rank}`} · 相似度{' '}
          {fmtScore(row.similarity)} · BM25 分 {fmtScore(row.bm25_score)} · RRF{' '}
          {fmtScore(row.rrf_score)}
          {reranked ? ` · logit ${fmtScore(row.rerank_score)}` : ''}
        </span>
      </div>
      <div className={styles.retrieverExcerptBody}>{row.content}</div>
    </div>
  );
}
