import { client } from './client';
import type { MetricInfo, MetricsQueryOut } from '../types/api';

/**
 * 语义层接口（D37 / PRD §9.10 · F11）——分析与评测页共用的指标取数层。
 *
 * 两个端点各回答一个问题：
 *   · GET  /api/metrics         "有哪些指标、每个怎么理解"（自描述目录）
 *   · POST /api/metrics/query   "取一个指标在某个时间窗的数"
 *
 * 为什么前端不硬编码指标口径：指标名 / 中文名 / 说明 / 维度白名单的唯一来源是
 * 后端 `metrics.yaml`（语义层设计），这里只做传输 —— UI 上任何口径说明文字都应
 * 从 `listMetrics()` 取或与之逐字一致，不在前端另立一套（"两个口径"是项目里
 * 反复踩过的坑型）。
 */

/** 可用指标清单（口径目录）。 */
export async function listMetrics(): Promise<MetricInfo[]> {
  const { data } = await client.get<MetricInfo[]>('/metrics');
  return data;
}

/**
 * 按「指标名 + 维度 + 时间窗」查询。
 *
 * 参数名从前端 camelCase 翻译成后端的 snake_case（start_date / end_date）——
 * 翻译只在这一层做，调用方（组件）不需要知道 HTTP 字段名。
 * `dimension` 不传 = 聚合查询（仅对 dimensions 为空的指标合法；传错会被后端 400
 * 并给出"可用取值"的 detail，错误信息直接可展示）。
 */
export async function queryMetric(input: {
  metric: string;
  dimension?: string;
  /** 起始日 YYYY-MM-DD（北京时间自然日，含当天） */
  startDate: string;
  /** 结束日 YYYY-MM-DD（北京时间自然日，含当天） */
  endDate: string;
}): Promise<MetricsQueryOut> {
  const { data } = await client.post<MetricsQueryOut>('/metrics/query', {
    metric: input.metric,
    ...(input.dimension ? { dimension: input.dimension } : {}),
    start_date: input.startDate,
    end_date: input.endDate,
  });
  return data;
}
