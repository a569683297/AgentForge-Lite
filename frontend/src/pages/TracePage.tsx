import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Select, Spin, Tooltip } from 'antd';
import { getTrace, getTraces } from '../api/traces';
import { EmptyState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';
import { StatusDot } from '../components/StatusDot';
import type {
  TraceDetailResponse,
  TraceKind,
  TraceSummaryItem,
} from '../types/api';
import styles from '../styles/dataPages.module.css';

const WINDOW_OPTIONS = [
  { value: 24, label: '最近 24 小时' },
  { value: 168, label: '最近 7 天' },
];

/** 类型的中文名。真值来源是后端的 kind 枚举，这里只负责翻译。 */
const KIND_LABEL: Record<TraceKind, string> = {
  dialog: '对话',
  inspect: '检索检查',
  eval: '评测',
  other: '其他',
};

/**
 * 列表条数。比原来的 20 放大，理由是**筛选已经在后端做了**：
 * 窗口内 eval-judge / retriever_inspect 的数量远多于 run_agent，
 * 如果仍在后端按时间截断 20 条，对话会被调试运行整体挤出去。
 */
const LIST_LIMIT = 50;

/** 墙上时间。跨天也要能读，所以固定带月日（7 天窗口里只显示时分秒会分不清）。 */
function fmtClock(value: string): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(
    at.getMinutes(),
  )}:${pad(at.getSeconds())}`;
}

function fmtDuration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}

/**
 * span 状态 → 视觉。
 *
 * 三态而不是两态：`warning` 的典型来源是**重排超时降级**（Langfuse 记
 * level=WARNING + statusMessage）。它不是失败 —— 只是退回融合序。
 * 染成红色会让人去查一个并不存在的故障，也会让真错误失去区分度。
 */
const SPAN_BAR_CLASS: Record<'ok' | 'warning' | 'error', string> = {
  ok: '',
  warning: styles.spanBarWarning,
  error: styles.spanBarError,
};
const SPAN_ROW_CLASS: Record<'ok' | 'warning' | 'error', string> = {
  ok: '',
  warning: styles.traceRowWarning,
  error: styles.traceRowError,
};
const SPAN_TONE: Record<'ok' | 'warning' | 'error', 'success' | 'warning' | 'danger'> = {
  ok: 'success',
  warning: 'warning',
  error: 'danger',
};

/**
 * Trace 时间线 —— **以提问为单位**。
 *
 * 数据来源：`GET /api/traces`（一次返回窗口内所有 span，后端按 traceId 分组）
 * + `GET /api/traces/{id}`（某一条的完整树）。
 *
 * 为什么从「一个下拉 + 一张图」改成「左列表 + 右瀑布」：
 *   下拉里每行只能写 `时刻 · run_agent · 耗时`，而 run_agent 这个名字**每次都一样** ——
 *   7 天里 20 次提问在下拉里长得完全一致，用户根本认不出哪条是哪次提问
 *   （这不是排版问题，是"没有身份标识"）。
 *   现在每行带提问原文，名字从 messages.trace_id 反查（见后端 `_load_questions`）。
 *
 * 为什么默认只看「对话」：
 *   同一个 Langfuse 项目里还混着检索检查器的手工查询和评测判分。
 *   实测 7 天窗口：eval-judge 120 条、retriever_inspect 49 条、run_agent 42 条 ——
 *   默认全看的话，前 50 条里几乎全是调试运行，用户要的对话根本不在第一屏。
 *   筛选在**后端**生效（先过滤再截断），前端 filter 做不到这件事。
 */
export function TracePage() {
  const [hours, setHours] = useState(24);
  const [kind, setKind] = useState<TraceKind | 'all'>('dialog');
  const [traces, setTraces] = useState<TraceSummaryItem[]>([]);
  const [kindCounts, setKindCounts] = useState<Partial<Record<TraceKind, number>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TraceDetailResponse | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [degraded, setDegraded] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 点过的 trace 不再重复拉：在几条之间来回比对是常态，
  // 每切一次就发一次 HTTP 既慢，也在白白消耗 Langfuse 的读取配额。
  const cache = useRef(new Map<string, TraceDetailResponse>());

  const loadList = useCallback(
    async (windowHours: number, nextKind: TraceKind | 'all') => {
      setListLoading(true);
      setError(null);
      try {
        const res = await getTraces(
          windowHours,
          LIST_LIMIT,
          nextKind === 'all' ? undefined : nextKind,
        );
        setDegraded(res.degraded ? (res.reason ?? '观测数据暂不可用') : null);
        setTruncated(res.truncated);
        setKindCounts(res.kind_counts ?? {});
        setTraces(res.traces);
        // 换窗口/换类型后原选中项可能不在新列表里 —— 那就落到新列表的第一条，
        // 而不是留一个查不到详情的 id（那会让右栏永远空着且无解释）。
        setSelected((prev) =>
          prev && res.traces.some((item) => item.trace_id === prev)
            ? prev
            : (res.traces[0]?.trace_id ?? null),
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : '加载失败');
        setTraces([]);
        setSelected(null);
        setDetail(null);
      } finally {
        setListLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadList(hours, kind);
  }, [hours, kind, loadList]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    const hit = cache.current.get(selected);
    if (hit) {
      setDetail(hit);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    getTrace(selected)
      .then((res) => {
        cache.current.set(selected, res);
        if (alive) setDetail(res);
      })
      .catch((err: unknown) => {
        if (alive) {
          setDetail(null);
          setError(err instanceof Error ? err.message : '加载失败');
        }
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    // 组件卸载或切到别的 trace 时丢弃这次结果：否则慢请求会覆盖新选中项的详情
    return () => {
      alive = false;
    };
  }, [selected]);

  const refresh = useCallback(() => {
    cache.current.clear();
    void loadList(hours, kind);
  }, [hours, kind, loadList]);

  const switchWindow = (value: number) => {
    cache.current.clear();
    setHours(value);
  };

  // 筛选器上带计数：用户能立刻知道"别的类型还有多少条"，
  // 而不用一个个点过去看。数字来自后端（过滤前统计），切类型时不会跟着缩水。
  const kindOptions = useMemo(() => {
    const total = Object.values(kindCounts).reduce<number>(
      (sum, value) => sum + (value ?? 0),
      0,
    );
    return [
      { value: 'all' as const, label: `全部类型（${total}）` },
      ...(Object.keys(KIND_LABEL) as TraceKind[]).map((key) => ({
        value: key,
        label: `${KIND_LABEL[key]}（${kindCounts[key] ?? 0}）`,
      })),
    ];
  }, [kindCounts]);

  // 列表与详情任一处降级，都按同一条提示展示（详情单独降级时列表可能还是好的）
  const degradedReason =
    degraded ?? (detail?.degraded ? (detail.reason ?? '观测数据暂不可用') : null);
  const total = detail && detail.total_ms > 0 ? detail.total_ms : 1;

  const otherKindCount = Object.entries(kindCounts)
    .filter(([key]) => key !== kind)
    .reduce<number>((sum, [, value]) => sum + (value ?? 0), 0);

  return (
    <div>
      <PageHeader
        title="Trace 时间线"
        description="每次提问一行 —— 点开看这一次运行里各段 span 的耗时分解（数据来自 Langfuse 实时观测）。"
        action={
          <Button icon={<ReloadOutlined />} loading={listLoading} onClick={refresh}>
            刷新
          </Button>
        }
      />

      {degradedReason ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="观测数据暂不可用"
          description={degradedReason}
        />
      ) : null}

      {error ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="加载失败"
          description={error}
        />
      ) : null}

      <div className={styles.traceToolbar}>
        <Select
          value={kind}
          options={kindOptions}
          onChange={(value: TraceKind | 'all') => setKind(value)}
          style={{ width: 176 }}
        />
        <Select
          value={hours}
          options={WINDOW_OPTIONS}
          onChange={switchWindow}
          style={{ width: 150 }}
        />
        {truncated ? (
          <Tooltip title="这个时间窗内的 span 超过单次查询上限（200 条），只拉了最近一批 —— 较早的运行可能不在这个列表里，耗时数字也可能偏小">
            <span className="af-muted">（已截断）</span>
          </Tooltip>
        ) : null}
      </div>

      <div className={styles.traceLayout}>
        <section className={styles.traceListPanel}>
          <div className={styles.traceListHead}>
            <span>
              {traces.length} 条{kind === 'all' ? '' : KIND_LABEL[kind]}
            </span>
            <span>{hours >= 48 ? '最近 7 天' : '最近 24 小时'}</span>
          </div>
          {listLoading && traces.length === 0 ? (
            <div className={styles.panelInlineEmpty}>
              <Spin size="small" />
            </div>
          ) : traces.length === 0 ? (
            <div className={styles.panelInlineEmpty}>这个筛选下没有记录</div>
          ) : (
            <div className={styles.traceList}>
              {traces.map((item) => (
                <TraceListItem
                  key={item.trace_id}
                  item={item}
                  active={item.trace_id === selected}
                  onSelect={() => setSelected(item.trace_id)}
                />
              ))}
            </div>
          )}
        </section>

        <section className={styles.traceDetailPanel}>
          {detailLoading ? (
            <div className={styles.panelInlineEmpty}>
              <Spin />
            </div>
          ) : detail && detail.spans.length ? (
            <>
              <p className={styles.traceQuestion}>
                {detail.question ?? (
                  <span className={styles.traceItemQuestionMissing}>
                    （这次运行没有记录提问原文 —— 改造前的历史对话，或评测运行）
                  </span>
                )}
              </p>
              <div className={styles.traceDetailMeta}>
                <span>{fmtClock(detail.start_time)}</span>
                <span className="af-num">总耗时 {fmtDuration(detail.total_ms)}</span>
                <span>{detail.span_count} 个 span</span>
                <span>
                  {KIND_LABEL[detail.kind]} · {detail.name}
                </span>
                {detail.session_id ? (
                  <span className="af-mono">会话 {detail.session_id.slice(0, 8)}</span>
                ) : null}
              </div>
              <div className={styles.traceWaterfall}>
                <div className={styles.traceAxis}>
                  <span>Span</span>
                  <span>时间轴 · 0ms — {Math.round(detail.total_ms)}ms</span>
                  <span className="af-num">耗时</span>
                </div>
                {detail.spans.map((span) => {
                  const left = Math.max(0, Math.min(100, (span.start_ms / total) * 100));
                  const width = Math.max(
                    1.2,
                    Math.min(100 - left, (span.duration_ms / total) * 100),
                  );
                  return (
                    <div
                      className={`${styles.traceRow} ${SPAN_ROW_CLASS[span.status]}`}
                      key={span.id}
                    >
                      <div className={styles.spanName} style={{ paddingLeft: span.depth * 14 }}>
                        <span className={styles.spanIndent} />
                        <span className="af-truncate" title={span.name}>
                          {span.name}
                        </span>
                        <span className="af-muted af-mono" style={{ fontSize: 10 }}>
                          {span.type.toLowerCase()}
                        </span>
                        <StatusDot tone={SPAN_TONE[span.status]} />
                      </div>
                      <div className={styles.traceTrack}>
                        <Tooltip
                          title={
                            <div>
                              {span.name}
                              <br />
                              {span.start_ms}–{span.end_ms}ms（{Math.round(span.duration_ms)}ms）
                              <br />
                              类型：{span.type}
                              {span.model ? ` · 模型：${span.model}` : ''}
                              {span.status_message ? (
                                <>
                                  <br />
                                  <span style={{ opacity: 0.85 }}>{span.status_message}</span>
                                </>
                              ) : null}
                            </div>
                          }
                        >
                          <span
                            className={`${styles.spanBar} ${SPAN_BAR_CLASS[span.status]}`}
                            style={{ left: `${left}%`, width: `${width}%` }}
                          />
                        </Tooltip>
                      </div>
                      <span className={`${styles.spanDuration} af-num`}>
                        {Math.round(span.duration_ms)}ms
                      </span>
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <EmptyState
              message={
                degradedReason
                  ? '观测数据暂不可用，恢复后点刷新即可。'
                  : kind === 'dialog' && otherKindCount > 0
                    ? `这个时间窗内没有对话记录；另有 ${otherKindCount} 条检索检查/评测运行，切到「全部类型」可以看到它们。`
                    : '这个时间窗内还没有 trace —— 去对话页聊一句就会产生一条。'
              }
              action="刷新"
              onAction={refresh}
            />
          )}
        </section>
      </div>
    </div>
  );
}

/** 左列表的一行 = 一次提问。 */
function TraceListItem({
  item,
  active,
  onSelect,
}: {
  item: TraceSummaryItem;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`${styles.traceItem} ${active ? styles.traceItemActive : ''}`}
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
    >
      <span className={styles.traceItemTop}>
        <span className={styles.traceItemClock}>{fmtClock(item.start_time)}</span>
        <span className={styles.traceItemMeta}>
          {fmtDuration(item.duration_ms)} · {item.span_count} span
        </span>
        <StatusDot tone={item.has_error ? 'danger' : 'muted'} />
      </span>
      <span
        className={`${styles.traceItemQuestion} ${
          item.question ? '' : styles.traceItemQuestionMissing
        }`}
      >
        {item.question ?? '（未记录提问）'}
      </span>
    </button>
  );
}
