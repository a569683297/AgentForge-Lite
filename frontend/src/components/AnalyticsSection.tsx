import { InfoCircleOutlined } from '@ant-design/icons';
import { Card, Segmented, Tooltip } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { queryMetric } from '../api/metrics';
import type { MetricRow, MetricsQueryOut } from '../types/api';
import { ErrorState } from './EmptyState';
import { InfoTipContent, type InfoTip } from './InfoTip';
import { MetricCard } from './MetricCard';
import { MetricChart } from './MetricChart';
import { TableSkeleton } from './Skeleton';
import styles from '../styles/dataPages.module.css';

/**
 * 分析区（块 4：由独立「分析」页合并进评测页，数据从 mock 换成语义层真数据）
 * =====================================================================
 * 六个组件、六个指标（全部走 `POST /api/metrics/query`，指标名/口径的唯一来源是后端 metrics.yaml）：
 *
 *   四卡： 会话数 session_count ┐
 *          消息数 message_count ├─ SQL 源（项目库）
 *          工具调用次数 tool_usage ┘（分组行求和，卡片侧口径，见卡上 info 说明）
 *          Token 消耗 token_cost ── Langfuse 源（**可能降级**，见下）
 *   双栏： 每日消息量 message_volume（dim: day）→ 曲线
 *          工具调用排行 tool_usage（dim: tool_name）→ 前 5 名 + 占比条
 *   全宽： 各阶段延迟 P95 latency_p95（dim: name）→ 条列表 + 样本数（Langfuse 源）
 *
 * 两条设计约定（都来自 PRD §9.10.5 的接口契约，不是本组件的自由发挥）：
 *   ① **双源返回结构一致**——所以这里六个查询用同一套渲染管线，不为 langfuse 源写特例；
 *   ② **langfuse 源会降级**（degraded=true + error，HTTP 仍 200）——降级只影响该指标：
 *      Token 卡与延迟面板显示「暂不可用」占位，其余四个 SQL 指标照常渲染。
 *      这不是错误路径，是正常业务状态（"外部依赖暂时不可用"），所以不弹错、不整区失败。
 *
 * 时间窗：近 7 天（默认）/ 近 30 天，切换后六个查询并行重取。
 * 窗口标注（footnote）与展示数据**绑在同一次响应里**（view 携带起止日期）——
 * 避免"切换后旧数据还挂着、标注却已更新"的口径错位。
 */

interface AnalyticsView {
  /** 这批数据实际对应的窗口（与展示强绑定，见组件头注释） */
  startDate: string;
  endDate: string;
  sessions: MetricsQueryOut;
  messages: MetricsQueryOut;
  tools: MetricsQueryOut;
  tokens: MetricsQueryOut;
  volume: MetricsQueryOut;
  latency: MetricsQueryOut;
}

// ── 口径说明（挂 info 悬浮）：与 metrics.yaml 的 description 保持一致，
//    "分组行求和"这两条是卡片自己做的聚合，更必须写在 UI 上（不能让人以为是后端原样返回）。

const cardTips: Record<'sessions' | 'messages' | 'tools' | 'tokens', InfoTip> = {
  sessions: {
    title: '会话数 · 时间窗内新建的对话会话',
    steps: [
      '来源：sessions 表，一行 = 一个会话（按创建时间落窗）',
      '时间窗为北京时间自然日 [起始, 结束] 闭区间',
    ],
    note: '与「消息数」的区别：一个会话通常包含多轮消息（一次提问至少 1 条 user + 1 条 assistant）。',
  },
  messages: {
    title: '消息数 · 时间窗内的消息条数',
    steps: [
      '来源：messages 表，全部角色都算（user / assistant / tool）',
      '工具调用会额外产生 tool 消息，所以它多于"用户问了几次"',
    ],
    note: '只统计已落库的消息；生成中途失败的请求不计。',
  },
  tools: {
    title: '工具调用次数 · 全部工具被调用的总次数',
    steps: [
      '来源：messages.tool_calls（每次调用逐条展开）',
      '口径 = 按工具名分组后的各行求和（与右侧「工具调用排行」同源）',
    ],
    note: '一轮可调用多个工具；tool_calls 为空的消息不贡献计数。',
  },
  tokens: {
    title: 'Token 消耗 · 时间窗内累计 token 总量',
    steps: [
      '来源：Langfuse 观测（按模型分组后 usage.totalTokens 求和）',
      '覆盖窗口内全部被追踪的 LLM 调用（对话生成等）',
    ],
    note: '未配置模型价目表，因此只有 token 量、没有金额；数据源不可达时本卡显示「暂不可用」。',
  },
};

const panelTips: Record<'volume' | 'toolRank' | 'latency', InfoTip> = {
  volume: {
    title: '每日消息量 · 对话量趋势',
    steps: [
      '按北京时间自然日分桶，逐日统计消息条数（含 user / assistant / tool）',
      '一个点 = 那一天的消息总数',
    ],
    note: '看趋势用——突起的峰通常对应一次评测跑批或一批调试对话。',
  },
  toolRank: {
    title: '工具调用排行 · 哪些工具真正在被用',
    steps: [
      '与「工具调用次数」卡同源（tool_usage 按工具名分组）',
      '按次数降序取前 5；条长按最大值归一化（看的是相对占比）',
    ],
    note: '长尾工具（只调用过一两次）不在前 5 内。',
  },
  latency: {
    title: '各阶段延迟 P95 · 哪一段慢',
    steps: [
      '来源：Langfuse，按 span 名称统计 P95 耗时（秒）',
      '阶段含 run_agent（整轮）/ retrieval / embedding / vector_search / bm25 / rerank / tool:*（各工具）',
      'P95 = 95% 的观测快于此值；右侧是参与统计的样本数',
    ],
    note: '样本很小时 P95 ≈ 最大值，读数是「观测值」而非统计结论——请结合样本数判断。',
  },
};

// ── 小工具（纯函数，组件内自足，不额外抽公共模块）─────────────────────

/** 本地日期串 YYYY-MM-DD（浏览器本地日期；语义层按北京时间自然日解释，对国内用户两者一致）。 */
function formatDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

/** 时间窗 [start, end]：end = 今天、start = 往前 days-1 天（首尾都含当天，与语义层闭区间语义一致）。 */
function dateRange(days: number): [string, string] {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - (days - 1));
  return [formatDate(start), formatDate(end)];
}

/** 枚举 [start, end] 的每一天（YYYY-MM-DD）。上限 60 天是防呆（正常窗口 7 / 30 天）。 */
function enumerateDays(start: string, end: string): string[] {
  const days: string[] = [];
  const cursor = new Date(`${start}T00:00:00`);
  const last = new Date(`${end}T00:00:00`);
  while (cursor <= last && days.length < 60) {
    days.push(formatDate(cursor));
    cursor.setDate(cursor.getDate() + 1);
  }
  return days;
}

/** 读一行的数值列；缺失 / 非数字一律 null —— 页面统一按「—」渲染，不猜 0。 */
function readNumber(row: MetricRow | undefined, key: string): number | null {
  const value = row?.[key];
  return typeof value === 'number' ? value : null;
}

/** 分组行求和（「工具调用次数」「Token 消耗」两卡的卡片侧口径）。空行集返回 null（= 无数据）。 */
function sumValues(rows: MetricRow[]): number | null {
  if (!rows.length) return null;
  let total = 0;
  for (const row of rows) total += readNumber(row, 'value') ?? 0;
  return total;
}

function formatCount(value: number | null): string {
  return value == null ? '—' : value.toLocaleString('zh-CN');
}

/** P95 秒值：两位数十位给 1 位小数，其余给 3 位（0.751s 这种精度损失不起）。 */
function formatSeconds(value: number | null): string {
  if (value == null) return '—';
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(3)}s`;
}

/** 降级原因的显示裁剪：完整原因可能是长连接串，note 只留一句。 */
function clipError(error: string | null): string {
  if (!error) return '数据源不可达，稍后自动恢复';
  return error.length > 60 ? `${error.slice(0, 60)}…` : error;
}

const WINDOW_OPTIONS = [
  { label: '近 7 天', value: 7 },
  { label: '近 30 天', value: 30 },
];

export function AnalyticsSection() {
  const [days, setDays] = useState(7);
  const [reloadKey, setReloadKey] = useState(0);
  const [startDate, endDate] = useMemo(() => dateRange(days), [days]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<AnalyticsView | null>(null);

  useEffect(() => {
    // 「已取消」标记：切换时间窗 / 组件卸载后，旧请求的响应不再写 state。
    // 没有它时快速来回切窗口，**先发后到**的旧响应会覆盖新窗口数据（不报错、只是数字悄悄错掉）。
    let cancelled = false;
    setLoading(true);
    setError(null);
    void Promise.all([
      queryMetric({ metric: 'session_count', startDate, endDate }),
      queryMetric({ metric: 'message_count', startDate, endDate }),
      queryMetric({ metric: 'tool_usage', dimension: 'tool_name', startDate, endDate }),
      queryMetric({ metric: 'token_cost', dimension: 'model', startDate, endDate }),
      queryMetric({ metric: 'message_volume', dimension: 'day', startDate, endDate }),
      queryMetric({ metric: 'latency_p95', dimension: 'name', startDate, endDate }),
    ])
      .then(([sessions, messages, tools, tokens, volume, latency]) => {
        if (cancelled) return;
        setView({ startDate, endDate, sessions, messages, tools, tokens, volume, latency });
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : '无法加载指标数据');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [startDate, endDate, reloadKey]);

  // 双栏数据的两处派生（都按「已就绪的 view」算，与窗口标注一致）
  const volumePoints = useMemo(() => {
    if (!view) return [];
    // 先建 日期 → 值 的映射，再**按窗口枚举补齐每一天（缺的填 0）**：
    // 语义层只返回"有数据的日期"（如某天没人用就没有那一行），而曲线的时间轴是连续的——
    // 跳过无数据的日期会让"隔天断档"看起来像"相邻两天连出的走势"，与事实不符。
    const byDay = new Map((view.volume.rows ?? []).map((row) => [String(row.dim ?? ''), readNumber(row, 'value') ?? 0]));
    return enumerateDays(view.startDate, view.endDate).map((day) => ({
      // x 轴只显示 MM/DD（与页面其他地方的时间显示密度一致）
      date: day.slice(5).replace('-', '/'),
      value: byDay.get(day) ?? 0,
    }));
  }, [view]);
  const toolRows = (view?.tools.rows ?? []).slice(0, 5);
  const toolMax = Math.max(1, ...toolRows.map((row) => readNumber(row, 'value') ?? 0));
  const latencyRows = view?.latency.rows ?? [];
  const latencyMax = Math.max(1, ...latencyRows.map((row) => readNumber(row, 'value') ?? 0));

  return (
    <section className={styles.analyticsSection}>
      <div className={styles.sectionHead}>
        <div>
          <h2 className={styles.sectionTitle}>分析</h2>
          <p className={styles.sectionDescription}>系统运行视角：对话用量、工具使用与阶段延迟（数据源：项目库 + Langfuse 追踪）。</p>
        </div>
        <div className={styles.sectionHeadRight}>
          {/* 切换窗口后的静默刷新标记：数据不闪（保留旧窗口结果直到新结果就绪），用一行小字提示"正在取数" */}
          {loading && view ? <span className={styles.sectionLoading}>刷新中…</span> : null}
          <Segmented options={WINDOW_OPTIONS} value={days} onChange={(value) => setDays(Number(value))} />
        </div>
      </div>

      {error ? (
        <ErrorState message={`分析数据加载失败：${error}`} action="重试" onAction={() => setReloadKey((key) => key + 1)} />
      ) : !view ? (
        <TableSkeleton rows={4} columns={4} />
      ) : (
        <>
          <div className={styles.metricGrid}>
            <MetricCard
              label="会话数"
              value={<span className="af-num">{formatCount(readNumber(view.sessions.rows[0], 'value'))}</span>}
              note="窗口内新建会话"
              labelTip={<InfoTipContent tip={cardTips.sessions} />}
            />
            <MetricCard
              label="消息数"
              value={<span className="af-num">{formatCount(readNumber(view.messages.rows[0], 'value'))}</span>}
              note="user / assistant / tool 全部角色"
              labelTip={<InfoTipContent tip={cardTips.messages} />}
            />
            <MetricCard
              label="工具调用次数"
              value={<span className="af-num">{formatCount(sumValues(view.tools.rows))}</span>}
              note="按工具名分组求和"
              labelTip={<InfoTipContent tip={cardTips.tools} />}
            />
            <MetricCard
              label="Token 消耗"
              value={view.tokens.degraded ? <span className={styles.degradedValue}>暂不可用</span> : <span className="af-num">{formatCount(sumValues(view.tokens.rows))}</span>}
              note={view.tokens.degraded ? clipError(view.tokens.error) : 'Langfuse · 全部被追踪的 LLM 调用'}
              labelTip={<InfoTipContent tip={cardTips.tokens} />}
            />
          </div>

          <div className={styles.analyticsGrid}>
            <Card
              className={`${styles.panel} ${styles.chartPanel}`}
              styles={{ body: { padding: 0 } }}
              title={<span className={styles.panelTitleLabel}>每日消息量<Tooltip title={<InfoTipContent tip={panelTips.volume} />}><span className={styles.panelTitleInfo}><InfoCircleOutlined /></span></Tooltip></span>}
            >
              <div className={styles.chartBody}>
                {volumePoints.length ? <MetricChart points={volumePoints} /> : <div className={styles.panelInlineEmpty}>窗口内没有消息记录。</div>}
              </div>
            </Card>
            <Card
              className={styles.panel}
              styles={{ body: { padding: 0 } }}
              title={<span className={styles.panelTitleLabel}>工具调用排行<Tooltip title={<InfoTipContent tip={panelTips.toolRank} />}><span className={styles.panelTitleInfo}><InfoCircleOutlined /></span></Tooltip></span>}
            >
              <div className={styles.rankList}>
                {toolRows.map((row, index) => (
                  <div className={styles.analyticsRank} key={String(row.dim)}>
                    <span className="af-num">{index + 1}</span>
                    <div>
                      <div className="af-mono">{String(row.dim ?? '—')}</div>
                      <div className={styles.rankProgress}>
                        <span style={{ width: `${((readNumber(row, 'value') ?? 0) / toolMax) * 100}%` }} />
                      </div>
                    </div>
                    <span className="af-num">{formatCount(readNumber(row, 'value'))}</span>
                  </div>
                ))}
                {!toolRows.length ? <div className={styles.panelInlineEmpty}>窗口内没有工具调用记录。</div> : null}
              </div>
            </Card>
          </div>

          <Card
            className={`${styles.panel} ${styles.latencyPanel}`}
            styles={{ body: { padding: 0 } }}
            title={<span className={styles.panelTitleLabel}>各阶段延迟 P95<Tooltip title={<InfoTipContent tip={panelTips.latency} />}><span className={styles.panelTitleInfo}><InfoCircleOutlined /></span></Tooltip></span>}
          >
            {view.latency.degraded ? (
              <div className={styles.panelInlineEmpty}>暂不可用 · {clipError(view.latency.error)}</div>
            ) : latencyRows.length ? (
              <div className={styles.latencyList}>
                {latencyRows.map((row) => (
                  <div className={styles.latencyRow} key={String(row.dim)}>
                    <span className={`af-mono ${styles.latencyName}`} title={String(row.dim ?? '')}>{String(row.dim ?? '—')}</span>
                    <div className={styles.latencyBar}>
                      <span style={{ width: `${((readNumber(row, 'value') ?? 0) / latencyMax) * 100}%` }} />
                    </div>
                    <span className={`af-num ${styles.latencyValue}`}>{formatSeconds(readNumber(row, 'value'))}</span>
                    <span className={`af-num ${styles.latencyCount}`}>样本 {readNumber(row, 'count') ?? '—'}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className={styles.panelInlineEmpty}>窗口内没有延迟观测。</div>
            )}
          </Card>

          <div className={styles.footnote}>
            数据窗口 {view.startDate} ~ {view.endDate}（北京时间自然日）· 指标口径见各标题旁的 info 说明
          </div>
        </>
      )}
    </section>
  );
}
