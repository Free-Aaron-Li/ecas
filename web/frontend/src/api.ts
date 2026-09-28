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
  /** 实际生效的时间窗（左闭右开，后端按自定义 start/end 或「最近 N 小时」算出来） */
  start: string | null
  end: string | null
  limit: number
  offset: number
  count: number
  items: Reading[]
}

export interface Summary {
  hours: number
  start: string | null
  end: string | null
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
  /** 当前用的是不是看板上的自定义值（false = 出厂默认） */
  custom: boolean
  /** 出厂默认值，供"恢复默认"按钮显示 */
  default_low: number | null
  default_high: number | null
}

/** 一条报警事件（进行中时 ended_ts 为空、active=true）。 */
export interface AlarmEvent {
  id: number
  kind: 'break' | 'over' | 'conc_high' | 'conc_low' | 'no_data'
  kind_label: string
  level: '严重' | '警告'
  started_ts: string
  ended_ts: string | null
  duration_seconds: number
  active: boolean
  peak_raw: number | null
  peak_conc: number | null
  peak_ts: string | null
  n_rows: number
  note: string | null
}

export interface AlarmsPage {
  hours: number
  start: string | null
  end: string | null
  count: number
  /** 其中还在进行中的条数 */
  active: number
  items: AlarmEvent[]
  /** 后端最近一次扫描的结果（没扫过就是 null） */
  scan: Record<string, unknown> | null
}

/**
 * 时间窗：要么给 `hours`（预设，终点=现在、随时间滑动），
 * 要么给 `start`/`end`（自定义时段，绝对时刻，刷新后不漂）。
 */
export interface TimeWindow {
  hours: number
  /** 组件里给用户看的标签（不进请求） */
  label?: string
  start?: string
  end?: string
}

/** 时间窗 → 查询串；自定义时段优先（后端四个接口都认这一套）。 */
export function windowQuery(win: TimeWindow): string {
  const p = new URLSearchParams()
  if (win.start && win.end) {
    p.set('start', win.start)
    p.set('end', win.end)
  } else {
    p.set('hours', String(win.hours))
  }
  return p.toString()
}

/** 网关会话（Cloudflare Worker 注入的 cookie 会话；直连后端时没有这个接口）。 */
export interface Session {
  user: string
  role: string
  must_change: boolean
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

/**
 * 网关会话：能拿到说明站点挂在账户网关后面（真令牌由网关注入，浏览器只有占位符）。
 * 直连后端（本机开发 5173 / 隧道）没有这个接口 ⇒ 返回 null，界面维持原来的令牌门逻辑。
 */
export async function fetchSession(): Promise<Session | null> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  try {
    const resp = await fetch(withToken('/api/session', token), { headers })
    if (!resp.ok) return null
    const body = (await resp.json()) as Partial<Session>
    if (!body || typeof body.user !== 'string') return null
    return { user: body.user, role: body.role ?? '', must_change: !!body.must_change }
  } catch {
    return null
  }
}

export const fetchParams = () => getJSON<Params>('/api/params')
export const fetchHealth = () => getJSON<Health>('/api/health')
export const fetchSummary = (win: TimeWindow) => getJSON<Summary>(`/api/summary?${windowQuery(win)}`)
export const fetchReadings = (win: TimeWindow, limit = 3000) =>
  getJSON<ReadingsPage>(`/api/readings?${windowQuery(win)}&limit=${limit}`)

// —— 报警事件与阈值（第 3 步）——

export const fetchAlarms = (win: TimeWindow, limit = 50) =>
  getJSON<AlarmsPage>(`/api/alarms?${windowQuery(win)}&limit=${limit}`)

/** 自定义报警上下限：落库（后端 alarm_config），事件判定与图表阈值线一起生效。 */
export async function saveAlarms(low: number, high: number): Promise<Alarms> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  const query = `low=${encodeURIComponent(String(low))}&high=${encodeURIComponent(String(high))}`
  const resp = await fetch(withToken(`/api/alarms/config?${query}`, token), { method: 'PUT', headers })
  if (resp.status === 401) throw new AuthError()
  if (resp.status === 400) throw new Error(((await resp.json()) as { detail?: string }).detail ?? '上下限不合法')
  if (!resp.ok) throw new Error(`保存失败：HTTP ${resp.status}`)
  return (await resp.json()) as Alarms
}

/** 恢复出厂默认上下限。 */
export async function resetAlarms(): Promise<Alarms> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  const resp = await fetch(withToken('/api/alarms/config', token), { method: 'DELETE', headers })
  if (resp.status === 401) throw new AuthError()
  if (!resp.ok) throw new Error(`恢复默认失败：HTTP ${resp.status}`)
  return (await resp.json()) as Alarms
}

/** 立刻扫一次（后端后台每分钟也会扫）。 */
export const scanAlarms = () => postJSON<Record<string, unknown>>('/api/alarms/scan')

async function postJSON<T>(path: string): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  const resp = await fetch(withToken(path, token), { method: 'POST', headers })
  if (resp.status === 401) throw new AuthError()
  if (!resp.ok) throw new Error(`${path} → HTTP ${resp.status}`)
  return (await resp.json()) as T
}

/**
 * 导出 CSV 的下载地址。走浏览器原生下载（`<a download>`），所以**只能**用查询串带令牌
 * ——`Authorization` 头给不了 `<a>`；非 ASCII 令牌由 `withToken()` 自动改走 `?token=`。
 * 时间窗与看板完全一致：看的是哪一段，导出的就是哪一段。
 */
export function exportCsvUrl(win: TimeWindow): string {
  return withToken(`/api/export.csv?${windowQuery(win)}`, getToken())
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

/** 完整日期时间（自定义时段要写清年月日，跨月跨年都不能含糊）。 */
export function fmtFull(ts: string): string {
  const d = new Date(ts)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${fmtTime(ts)}`
}

/** ISO → `<input type="datetime-local">` 用的本地时间串（`YYYY-MM-DDTHH:mm`）。 */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** `<input type="datetime-local">` 的值 → 带时区的 ISO（后端只认 ISO，且要能对得上绝对时刻）。 */
export function fromLocalInput(value: string): string {
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '' : d.toISOString()
}

export function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

/**
 * 补偿系数这种人看的数字：去掉无意义的尾零。
 * ⚠️ 别用 fmtNum(v, 1) —— +0.48 会被四舍五入成「+0.5」，看板上就会写着 +0.5 而实际按 +0.48 算。
 */
export function trimNum(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return String(Number(v.toFixed(digits)))
}

/** 把现役补偿口径写成一条能直接照抄的公式（如 `conc_raw + 0.48`）。 */
export function concFormula(params: Params | null): string {
  if (!params) return 'conc_raw + offset_conc'
  if (params.offset_mode === 'mul') {
    return `conc_raw × ${(1 + params.offset_pct / 100).toFixed(4)}`
  }
  return `conc_raw + ${trimNum(params.offset_conc)}`
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s} 秒`
  if (s < 5400) return `${(s / 60).toFixed(1)} 分钟`
  if (s < 172800) return `${(s / 3600).toFixed(1)} 小时`
  return `${(s / 86400).toFixed(1)} 天`
}
