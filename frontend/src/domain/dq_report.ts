/**
 * 质量报告跨版本适配：v1 汇总行和 v2 快照同时存在时，页面统一按
 * “报告编号 + 实际取得报告”展示，不在前端猜哪个字段该用哪一版。
 */

export type LegacyMetricSource =
  | { 模块?: string; 字段?: string; 时点?: number; 日报日期?: string; 结果?: string; 检索日期?: string; 检索?: string } & Record<string, unknown>

export type SnapshotMetric = {
  名称: string
  原值: string | number | null
  值: string | number | null
  重算值: string | number | null
  来源: LegacyMetricSource
  可重做: boolean
  重做状态: '待重做' | '已重做' | '存量兼容缺项' | '生成时计算' | string
  是否缺项: boolean
  同步记录?: string
}

export type IncidentEvidence = {
  事件ID?: number
  事件编号?: string
  触发时间?: string
  严重级别?: string
  当时状态?: string
  当时处理状态?: string
  处置归属?: string
  处置状态?: string
  关联规则?: string
  来源时点?: number
}

export type ReportSnapshot = {
  id: number
  快照编号: string
  报告编号: string
  日报日期: string | null
  结构版本: string
  来源版本: string
  迁移编号: string | null
  数据时点: number
  快照时间: string
  提交状态: string
  签收状态: string
  修订号: number
  前序快照: number | null
  指标: SnapshotMetric[]
  缺项: string[]
  缺项标记: string
  补齐率: number
  完整: boolean
  事件回查: IncidentEvidence[]
}

export type EffectiveReport = {
  报告编号: string
  来源结构: 'v1' | 'v2.0' | '不存在' | string
  可取得: boolean
  日报日期?: string | null
  快照编号?: string
  修订号?: number
  签收状态?: string
  报告: Record<string, string | number | null | undefined>
  字段来源?: Record<string, { 结构版本: string; 回退: boolean; 值?: unknown; 重做状态?: string }>
  补齐率: number
  缺项: string[]
  缺项标记?: string
  回退项?: string[]
  完整?: boolean
  事件回查: IncidentEvidence[]
  说明: string
}

export type VersionHistory = {
  结构版本: string
  类型: '存量汇总行' | '报告快照'
  报告编号: string
  日报日期?: string | null
  快照编号?: string
  修订号?: number
  签收状态?: string
  补齐率?: number
  缺项?: string[]
  可回查: boolean
}

export type ReportVersions = {
  报告编号: string
  原编号核对: boolean
  版本数: number
  历史: VersionHistory[]
  最新有效: EffectiveReport
  说明: string
}

/** 缺项统一显示成便于核对的短语，避免页面各处各写一遍。 */
export function missingLabel(snapshot: Pick<ReportSnapshot, '缺项' | '缺项标记'>): string {
  if (!snapshot.缺项?.length) return '无缺项'
  return `${snapshot.缺项标记 || '存量兼容缺项'}：${snapshot.缺项.join('、')}`
}

/** 事件证据的“当时状态”和“当前状态”要分开显示，不能混成一个状态。 */
export function evidenceStatus(evidence: IncidentEvidence, current?: string): string {
  const then = evidence.当时状态 ?? '未知'
  return current === undefined ? then : `当时 ${then} / 当前 ${current}`
}

/** 已签收快照不允许直接改，只能逐项重做生成新修订。 */
export function canEditSnapshot(snapshot: Pick<ReportSnapshot, '签收状态'>): boolean {
  return snapshot.签收状态 !== '已签收' && snapshot.签收状态 !== '已作废'
}
