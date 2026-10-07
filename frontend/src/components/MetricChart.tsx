import ReactECharts from 'echarts-for-react';
import { useTheme } from '../theme/ThemeProvider';
import { palette } from '../theme/palette';

export interface MetricPoint { date: string; value: number; }

export function MetricChart({ points }: { points: MetricPoint[] }) {
  const { mode } = useTheme();
  const p = palette[mode];
  const option = {
    animation: false,
    color: [p.accent],
    grid: { top: 12, right: 14, bottom: 28, left: 42 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: points.map((point) => point.date), axisLine: { lineStyle: { color: p.borderSubtle } }, axisLabel: { color: p.textTertiary, fontSize: 11 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: p.borderSubtle } }, axisLabel: { color: p.textTertiary, fontSize: 11 } },
    series: [{ type: 'line', smooth: false, symbol: 'circle', symbolSize: 5, data: points.map((point) => point.value), lineStyle: { width: 2 }, areaStyle: { opacity: 0.08 } }],
  };
  return <ReactECharts key={mode} option={option} style={{ height: 220, width: '100%' }} />;
}
