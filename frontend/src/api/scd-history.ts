import { request } from './client'

export type HistoryRole = {
  key: string
  label: string
  can_publish: boolean
  can_correct: boolean
}

export type HistoryEntry = {
  id: number
  履历ID: string
  目标维度: string
  变更字段: string
  旧值: string
  新值: string
  业务日期: string
  登记时间: string
  记录来源: string
  更正来源: string
  更正履历ID: string
  迟到: boolean
  是否只读: boolean
  登记人: string
  变更原因: string
  生效状态: string
  被更正为?: string
}

export type Interval = {
  id: number
  区间ID: string
  字段: string
  值: string
  生效开始: string
  生效结束: string
  开放区间: boolean
  来源履历ID: string
  来源记录来源: string
  来源值状态: string
  修订来源: string
  后续更正: string[]
  上一值: string
  当前标记: string
  版本号: number
}

export type DimensionSummary = {
  维度编码: string
  维度名称: string
  SCD策略: string
  SCD类型: string
  策略生效时间: string
  履历数: number
  更正数: number
  区间数: number
  版本号: number
  最近业务日期: string
}

export type AsOfField = {
  字段: string
  值: string
  生效开始: string
  生效结束: string
  来源履历ID: string
  来源记录来源: string
  来源业务日期: string
  修订来源: string
  后续更正: string[]
  开放区间?: boolean
  关联更正: CorrectionLink[]
  下一变更: { 履历ID: string; 业务日期: string; 旧值: string; 新值: string; 记录来源: string } | null
  状态: string
}

export type CorrectionLink = {
  履历ID: string
  变更字段: string
  业务日期: string
  旧值: string
  新值: string
  记录来源: string
  更正来源: string
  登记时间: string
  关联理由: string[]
  是否只读: boolean
}

export type AsOfResult = {
  目标维度: string
  维度名称: string
  日期: string
  记录: Record<string, string>
  字段归属: AsOfField[]
  未覆盖字段: string[]
  关联更正: CorrectionLink[]
  版本标识: string
  说明: string
}

export type IntegrityResult = {
  目标维度: string
  版本号: number
  履历数: number
  更正数: number
  区间数: number
  ok: boolean
  overlaps: { 字段: string; 左区间: string; 右区间: string; 重叠开始: string; 重叠结束: string }[]
  gaps: { 字段: string; 缺口开始: string; 缺口结束: string }[]
  continuity_breaks: { 字段: string; 业务日期: string; 履历ID: string; 说明: string }[]
  same_day_conflicts: { 字段: string; 日期: string; 生效履历ID: string; 被覆盖履历ID: string }[]
  readonly_violations: { 履历ID: string; 问题: string }[]
  drift: { 区间ID: string; 问题: string }[]
  superseded: { 字段: string; 履历ID: string; 原因: string }[]
  warnings: string[]
}

export type AppendPayload = {
  field: string
  newValue: string
  oldValue?: string
  businessDate: string
  recordType: string
  reason?: string
  operator: string
  correctionSource?: string
  expectedRevision?: number
}

async function read<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : detail?.message
    throw new Error(message || `接口返回 ${response.status}`)
  }
  return payload as T
}

export async function listDimensions(): Promise<DimensionSummary[]> {
  return (await read<{ items: DimensionSummary[] }>('/api/scd-history/dimensions')).items
}

export async function listRoles(): Promise<HistoryRole[]> {
  return (await read<{ items: HistoryRole[] }>('/api/scd-history/roles')).items
}

export async function listHistory(
  dim: string,
  field?: string,
  includeCorrections = true,
): Promise<{ items: HistoryEntry[]; revision: number }> {
  const query = new URLSearchParams()
  if (field) query.set('field', field)
  query.set('include_corrections', String(includeCorrections))
  const path = `/api/scd-history/${encodeURIComponent(dim)}/history?${query.toString()}`
  return read<{ items: HistoryEntry[]; revision: number }>(path)
}

export async function listIntervals(dim: string, field?: string): Promise<Interval[]> {
  const query = field ? `?field=${encodeURIComponent(field)}` : ''
  const path = `/api/scd-history/${encodeURIComponent(dim)}/intervals${query}`
  return (await read<{ items: Interval[] }>(path)).items
}

export async function loadAsOf(dim: string, day: string, field?: string): Promise<AsOfResult> {
  const query = new URLSearchParams({ date: day })
  if (field) query.set('field', field)
  return read<AsOfResult>(`/api/scd-history/${encodeURIComponent(dim)}/as-of?${query.toString()}`)
}

export async function loadIntegrity(dim: string): Promise<IntegrityResult> {
  return read<IntegrityResult>(`/api/scd-history/${encodeURIComponent(dim)}/integrity`)
}

export async function appendHistory(
  dim: string,
  payload: AppendPayload,
): Promise<{ message: string; revision: number; late: boolean }> {
  return read(`/api/scd-history/${encodeURIComponent(dim)}/history`, {
    method: 'POST',
    body: JSON.stringify({
      字段: payload.field,
      新值: payload.newValue,
      旧值: payload.oldValue ?? '',
      业务日期: payload.businessDate,
      记录来源: payload.recordType,
      变更原因: payload.reason ?? '',
      更正来源: payload.correctionSource ?? '',
      登记人: payload.operator,
      期望版本: payload.expectedRevision ?? null,
    }),
  })
}
