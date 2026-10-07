import { BarChartOutlined } from '@ant-design/icons';
import { Card } from 'antd';
import { MetricCard } from '../components/MetricCard';
import { MetricChart } from '../components/MetricChart';
import { PageHeader } from '../components/PageHeader';
import { MOCK_ANALYTICS_SUMMARY, MOCK_METRICS } from '../mock';
import styles from '../styles/dataPages.module.css';

/**
 * 注意：数据源缺口（属 `IMPL-SPEC.md` §6.3 登记的三页之一）
 *
 * 后端目前**没有** `GET /api/metrics`。真实数据源排在：
 *   · **D35** —— 语义层 `metrics.yaml` + `metrics_service.py` + `query_metric` 工具
 *   · **D36** —— `AnalyticsPage` 接真数据（PRD §13.2 的 D36 行）
 * 所以 M6（D29–D33）只交付**界面骨架**：数字与曲线全部来自 `src/mock/index.ts`，页头挂「示例数据」徽标。
 * 接线时**只需替换 `src/mock/index.ts` 的两处导出**，本文件不需要改结构。
 */
export function AnalyticsPage() {
  return (
    <div>
      <PageHeader title="分析" description="观察 Agent 的调用成本、延迟与运行稳定性。" badge="示例数据" action={<BarChartOutlined />} />
      <div className={styles.footnote}>
        数据源 <span className="af-mono">GET /api/metrics</span> 属 <strong>D35 语义层</strong>（尚未实现），
        以下卡片与曲线均为<strong>示例数据</strong>，不代表系统真实运行结果；接线点在 <span className="af-mono">src/mock/index.ts</span>。
      </div>
      <div className={styles.metricGrid}><MetricCard label="总请求" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.totalRequests.toLocaleString()}</span>} note="等 D35 GET /api/metrics" /><MetricCard label="平均延迟" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.averageLatencyMs}</span>} suffix="ms" note="示例运行窗口" /><MetricCard label="Token 消耗" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.tokenUsage}</span>} note="输入 + 输出" /><MetricCard label="成功率" value={<span className="af-num">{MOCK_ANALYTICS_SUMMARY.successRate}</span>} note="示例运行窗口" /></div>
      <div className={styles.analyticsGrid}><Card className={`${styles.panel} ${styles.chartPanel}`} styles={{ body: { padding: 0 } }} title={<span>每日平均延迟</span>}><div className={styles.chartBody}><MetricChart points={MOCK_METRICS[0].points} /></div></Card><Card className={styles.panel} styles={{ body: { padding: 0 } }} title={<span>热门工具</span>}><div className={styles.rankList}>{MOCK_ANALYTICS_SUMMARY.popularTools.map((tool, index) => <div className={styles.analyticsRank} key={tool.name}><span className="af-num">{index + 1}</span><div><div className="af-mono">{tool.name}</div><div className={styles.rankProgress}><span style={{ width: `${(tool.count / MOCK_ANALYTICS_SUMMARY.popularTools[0].count) * 100}%` }} /></div></div><span className="af-num">{tool.count}</span></div>)}</div></Card></div>
    </div>
  );
}
