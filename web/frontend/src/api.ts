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
  /** 越限行数（只在有效区间里数） */
  n_below_alarm: number
  n_above_alarm: number
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

export interface Alarms {
  low: number
  high: number
  /** 上下限翻译成 raw 之后的判据（后端换算，前端不重算） */
  low_raw: number
  high_raw: number
}

export interface Params {
  model: string
  range: { i_lo: number; i_hi: number; c_lo: number; c_hi: number }
  offset_mode: 'add' | 'mul'
  offset_conc: number
  offset_pct: number
  thresholds: { break_low: number; over_high: number }
  alarms: Alarms
  stale_seconds: number
  export_limit: number
  formula: Record<string, string>
}

export interface AuthInfo {
  service: string
  auth_required: boolean
}

// —— 访问令牌 ——
// 存在 localStorage（键名与后端 Cookie 同名）；**只**通过 `Authorization` 头或
// `?token=` 传给后端，前端不做任何校验判断。

const TOKEN_KEY = 'ecas_token'

/** 后端要求令牌但没给对时抛这个，界面据此弹输入框。 */
export class AuthError extends Error {
  constructor(message = '需要访问令牌') {
    super(message)
    this.name = 'AuthError'
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(value: string): void {
  const v = value.trim()
  try {
    if (v) localStorage.setItem(TOKEN_KEY, v)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 隐私模式下写不进去就算了，本次会话内仍可用 */
  }
}

/** HTTP 头与 Cookie 按协议只能是 ASCII；非 ASCII 令牌只能走 `?token=`。 */
function asciiOnly(s: string): boolean {
  return /^[\x20-\x7e]*$/.test(s)
}

function withToken(path: string, token: string): string {
  if (!token || asciiOnly(token)) return path
  return `${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

/** 给 WebSocket 用：浏览器不能给 WS 带请求头，只能把令牌放查询串。 */
export function wsUrl(path: string, token: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${proto}://${window.location.host}${path}${query}`
}

async function getJSON<T>(path: string): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  const resp = await fetch(withToken(path, token), { headers })
  if (resp.status === 401) throw new AuthError()
  if (!resp.ok) throw new Error(`${path} → HTTP ${resp.status}`)
  return (await resp.json()) as T
}

export const fetchAuthInfo = () => getJSON<AuthInfo>('/api/auth')

export const fetchParams = () => getJSON<Params>('/api/params')
export const fetchHealth = () => getJSON<Health>('/api/health')
export const fetchSummary = (hours: number) => getJSON<Summary>(`/api/summary?hours=${hours}`)
export const fetchReadings = (hours: number, limit = 3000) =>
  getJSON<ReadingsPage>(`/api/readings?hours=${hours}&limit=${limit}`)

/**
 * 导出 CSV 的下载地址。走浏览器原生下载（`<a download>`），所以**只能**用查询串带令牌
 * ——`Authorization` 头给不了 `<a>`；非 ASCII 令牌由 `withToken()` 自动改走 `?token=`。
 */
export function exportCsvUrl(hours: number): string {
  return withToken(`/api/export.csv?hours=${hours}`, getToken())
}

/** 判断某个浓度是否越限（用后端给的报警上下限）。 */
export function alarmSide(conc: number, alarms: Alarms | null | undefined): 'low' | 'high' | null {
  if (!alarms) return null
  if (conc < alarms.low) return 'low'
  if (conc > alarms.high) return 'high'
  return null
}

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
