import { client } from './client';
import type {
  EvalCaseDetailOut,
  EvalCaseOut,
  EvalCaseResultDetailOut,
  EvalCaseResultOut,
  EvalDatasetOut,
  EvalRunDetailOut,
  EvalRunOut,
} from '../types/api';

export async function listEvalRuns(limit = 50): Promise<EvalRunOut[]> {
  const { data } = await client.get<EvalRunOut[]>('/eval/runs', { params: { limit } });
  return data;
}

export async function getEvalRun(runId: number): Promise<EvalRunDetailOut> {
  const { data } = await client.get<EvalRunDetailOut>(`/eval/runs/${runId}`);
  return data;
}

export async function listEvalRunCases(
  runId: number,
  params: { category?: string; passed?: boolean } = {},
): Promise<EvalCaseResultOut[]> {
  const { data } = await client.get<EvalCaseResultOut[]>(`/eval/runs/${runId}/cases`, { params });
  return data;
}

export async function getEvalCaseResult(runId: number, caseKey: string): Promise<EvalCaseResultDetailOut> {
  const { data } = await client.get<EvalCaseResultDetailOut>(`/eval/runs/${runId}/results/${caseKey}`);
  return data;
}

export async function getEvalDataset(): Promise<EvalDatasetOut> {
  const { data } = await client.get<EvalDatasetOut>('/eval/dataset');
  return data;
}

export async function listEvalCases(params: { category?: string } = {}): Promise<EvalCaseOut[]> {
  const { data } = await client.get<EvalCaseOut[]>('/eval/cases', { params });
  return data;
}

export async function getEvalCase(caseKey: string): Promise<EvalCaseDetailOut> {
  const { data } = await client.get<EvalCaseDetailOut>(`/eval/cases/${caseKey}`);
  return data;
}
