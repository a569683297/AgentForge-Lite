import { ReloadOutlined } from '@ant-design/icons';
import { Button, Select, Tooltip } from 'antd';
import { MOCK_TRACES } from '../mock';
import { PageHeader } from '../components/PageHeader';
import { StatusDot } from '../components/StatusDot';
import styles from '../styles/dataPages.module.css';

export function TracePage() {
  const trace = MOCK_TRACES[0];
  return (
    <div>
      <PageHeader title="Trace 时间线" description="按时间比例查看一次 Agent 运行中的父子 span。" badge="示例数据" action={<Button icon={<ReloadOutlined />}>刷新</Button>} />
      <div className={styles.traceToolbar}><Select value={trace.trace_id} options={MOCK_TRACES.map((item) => ({ value: item.trace_id, label: `${item.trace_id} · ${item.name}` }))} style={{ width: 360 }} /><Select value="最近 24 小时" options={[{ value: '最近 24 小时', label: '最近 24 小时' }]} /><span className="af-muted af-mono">总耗时 {trace.total_ms}ms</span></div>
      <div className={styles.traceWaterfall}>
        <div className={styles.traceAxis}><span>Span</span><span>时间轴 · 0ms — {trace.total_ms}ms</span><span className="af-num">耗时</span></div>
        {trace.spans.map((span) => {
          const left = (span.start_ms / trace.total_ms) * 100;
          const width = Math.max(1.5, ((span.end_ms - span.start_ms) / trace.total_ms) * 100);
          return <div className={`${styles.traceRow} ${span.status === 'error' ? styles.traceRowError : ''}`} key={span.id}><div className={styles.spanName} style={{ paddingLeft: span.depth * 14 }}><span className={styles.spanIndent} /> <span className="af-truncate">{span.name}</span><StatusDot tone={span.status === 'error' ? 'danger' : 'success'} /></div><div className={styles.traceTrack}><Tooltip title={<div>起止 {span.start_ms}–{span.end_ms}ms<br />输入：{span.input}<br />输出：{span.output}<br />tokens：{span.tokens}</div>}><span className={`${styles.spanBar} ${span.status === 'error' ? styles.spanBarError : ''}`} style={{ left: `${left}%`, width: `${width}%` }} /></Tooltip></div><span className={`${styles.spanDuration} af-num`}>{span.end_ms - span.start_ms}ms</span></div>;
        })}
      </div>
    </div>
  );
}
