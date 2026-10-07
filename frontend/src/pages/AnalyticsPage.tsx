import { BarChartOutlined } from '@ant-design/icons';
import { Card } from 'antd';
import { MetricCard } from '../components/MetricCard';
import { MetricChart } from '../components/MetricChart';
import { PageHeader } from '../components/PageHeader';
import { MOCK_ANALYTICS_SUMMARY, MOCK_METRICS } from '../mock';
import styles from '../styles/dataPages.module.css';

export function AnalyticsPage() {
  return (
    <div>
      <PageHeader title="分析" description="观察 Agent 的调用成本、延迟与运行稳定性。" badge="示例数据" action={<BarChartOutlined />} />
      <div className={styles.metricGrid}><MetricCard label="总请求" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.totalRequests.toLocaleString()}</span>} note="等后端 GET /api/metrics" /><MetricCard label="平均延迟" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.averageLatencyMs}</span>} suffix="ms" note="最近 7 天" /><MetricCard label="Token 消耗" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.tokenUsage}</span>} note="输入 + 输出" /><MetricCard label="成功率" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.successRate}</span>} note="mock 运行窗口" /></div>
      <div className={styles.analyticsGrid}><Card className={`${styles.panel} ${styles.chartPanel}`} bodyStyle={{ padding: 0 }} title={<span>每日平均延迟</span>}><div className={styles.chartBody}><MetricChart points={MOCK_METRICS[0].points} /></div></Card><Card className={styles.panel} bodyStyle={{ padding: 0 }} title={<span>热门工具</span>}><div className={styles.rankList}>{MOCK_ANALYTICS_SUMMARY.popularTools.map((tool, index) => <div className={styles.analyticsRank} key={tool.name}><span className="af-num">{index + 1}</span><div><div className="af-mono">{tool.name}</div><div className={styles.rankProgress}><span style={{ width: `${(tool.count / MOCK_ANALYTICS_SUMMARY.popularTools[0].count) * 100}%` }} /></div></div><span className="af-num">{tool.count}</span></div>)}</div></Card></div>
    </div>
  );
}
