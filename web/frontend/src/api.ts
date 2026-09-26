// 后端接口（web/backend/app/schemas.py）的 TypeScript 镜像契约。
// 换算字段全部由后端给出，前端只做展示与格式化，不重复实现口径。

export type Status = '正常' | '偏低' | '断线' | '超量程'

export interface Reading {
  ts: string
  model: string
  raw: number
  ma: number
  conc_raw: number
  conc: number
  status: Status
}

export interface ReadingsPage {
  hours: number
  limit: number
  offset: number
  count: number
  items: Reading[]
}

export interface Summary {
  hours: number
  n: number
  n_valid: number
  broken: number
  low: number
  over: number
  conc_avg: number | null
  conc_min: number | null
  conc_max: number | null
  raw_min: number | null
  raw_max: number | null
}

export interface Health {
  rows_total: number
  last_ts: string | null
  age_seconds: number | null
  stale: boolean
  last: Reading | null
}

export interface Params {
  model: string
  range: { i_lo: number; i_hi: number; c_lo: number; c_hi: number }
  offset_mode: 'add' | 'mul'
  offset_conc: number
  offset_pct: number
  thresholds: { break_low: number; over_high: number }
  stale_seconds: number
  formula: Record<string, string>
}

async function getJSON<T>(path: string): Promise<T> {
  const resp = await fetch(path, { headers: { accept: 'application/json' } })
  if (!resp.ok) throw new Error(`${path} → HTTP ${resp.status}`)
  return (await resp.json()) as T
}

export const fetchParams = () => getJSON<Params>('/api/params')
export const fetchHealth = () => getJSON<Health>('/api/health')
export const fetchSummary = (hours: number) => getJSON<Summary>(`/api/summary?hours=${hours}`)
export const fetchReadings = (hours: number, limit = 3000) =>
  getJSON<ReadingsPage>(`/api/readings?hours=${hours}&limit=${limit}`)

// —— 展示用格式化 ——

export function pillClass(status: Status | string): string {
  switch (status) {
    case '正常':
      return 'ok'
    case '偏低':
      return 'warn'
    case '断线':
      return 'bad'
    case '超量程':
      return 'over'
    default:
      return 'dim'
  }
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

export function fmtTime(ts: string): string {
  const d = new Date(ts)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function fmtDateTime(ts: string): string {
  const d = new Date(ts)
  return `${d.getMonth() + 1}/${d.getDate()} ${fmtTime(ts)}`
}

export function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s} 秒`
  if (s < 5400) return `${(s / 60).toFixed(1)} 分钟`
  if (s < 172800) return `${(s / 3600).toFixed(1)} 小时`
  return `${(s / 86400).toFixed(1)} 天`
}
