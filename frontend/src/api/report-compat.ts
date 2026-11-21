/** 质量报告兼容层请求封装：与后端 /api/report/* 一一对应。 */
import { request } from './client'
import type {
  EffectiveReport,
  ReportSnapshot,
  ReportVersions,
} from '@/domain/dq_report'

async function asJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback
    try {
      const body = await response.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      // 保留兜底文案
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export async function migrateLegacyReports(
  reportNumbers: string[] = [],
  stopAfter?: number,
): Promise<Record<string, unknown>> {
  const response = await request('/api/report/migrate', {
    method: 'POST',
    body: JSON.stringify({
      report_numbers: reportNumbers.length ? reportNumbers : null,
      stop_after: stopAfter ?? null,
    }),
  })
  return asJson<Record<string, unknown>>(response, '存量迁移失败')
}

export async function listSnapshots(
  reportNo: string,
): Promise<{ items: ReportSnapshot[]; total: number }> {
  const query = new URLSearchParams({ report_no: reportNo }).toString()
  return asJson(await request(`/api/report/snapshots?${query}`), '报告快照读取失败')
}

export async function reportVersions(reportNo: string): Promise<ReportVersions> {
  return asJson(await request(`/api/report/numbers/${encodeURIComponent(reportNo)}`), '报告版本读取失败')
}

export async function effectiveReport(reportNo: string): Promise<EffectiveReport> {
  return asJson(
    await request(`/api/report/numbers/${encodeURIComponent(reportNo)}/effective`),
    '报告跨版本读取失败',
  )
}

export async function recomputeSnapshot(
  snapshotId: number,
  metrics: string[],
  reviewer: string,
): Promise<{ ok: boolean; message: string; snapshot: ReportSnapshot }> {
  const response = await request(`/api/report/snapshots/${snapshotId}/recompute`, {
    method: 'POST',
    body: JSON.stringify({ metrics, reviewer }),
  })
  return asJson(response, '逐项重做失败')
}

export async function signSnapshot(
  snapshotId: number,
  result: '通过' | '驳回',
  reviewer: string,
  remark = '',
): Promise<{ ok: boolean; message: string; snapshot: ReportSnapshot }> {
  const response = await request(`/api/report/snapshots/${snapshotId}/sign`, {
    method: 'POST',
    body: JSON.stringify({ result, reviewer, remark }),
  })
  return asJson(response, '报告签收失败')
}
