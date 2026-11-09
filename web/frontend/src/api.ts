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
  /** 本次返回的行数（= items.length） */
  count: number
  /** 窗口内的**总**行数：count < rows_total 说明被 limit 截断或抽样过 */
  rows_total: number
  /** 走的是 `sample=auto` 的整窗抽样（items 均匀覆盖整个窗口，而不是最新 limit 条） */
  sampled: boolean
  /** 抽样时一个点代表多长时间（秒）；没抽样是 null */
  bucket_seconds: number | null
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
/** 一台已绑定的登录设备（网关维护，cookie `dm_dev` 认机器）。 */
export interface SessionDevice {
  id: string
  name: string
  ip: string
  first: string
  last: string
  current: boolean
}

export interface Session {
  user: string
  /** 中文姓名（后端 web_account.display_name；没填就是空串） */
  name?: string
  role: string
  must_change: boolean
  /** 当前这台设备的 id */
  device?: string
  /** 该账户最多可绑定几台设备（普通 5、admin 1） */
  limit?: number
  devices?: SessionDevice[]
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

/**
 * 会话被顶掉：同一账户在另一台设备登录（网关侧单点在线），本设备必须退出。
 * 与 AuthError 的区别很重要——令牌错误是「本机没带令牌」，这个是「你被挤下线了」。
 */
export class SessionRevokedError extends Error {
  constructor(message = '您的账号已在另一台设备登录，本设备已被强制退出') {
    super(message)
    this.name = 'SessionRevokedError'
  }
}

/** 401 时区分「没登录」与「被顶掉」：网关会给出 revoked。 */
async function authFailure(resp: Response): Promise<never> {
  try {
    const body = (await resp.json()) as { error?: string; message?: string }
    if (body?.error === 'revoked') throw new SessionRevokedError(body.message || undefined)
  } catch (err) {
    if (err instanceof SessionRevokedError) throw err
  }
  throw new AuthError()
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
  if (resp.status === 401) await authFailure(resp)
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
    return {
      user: body.user,
      name: typeof body.name === 'string' ? body.name : '',
      role: body.role ?? '',
      must_change: !!body.must_change,
      device: typeof body.device === 'string' ? body.device : undefined,
      limit: typeof body.limit === 'number' ? body.limit : undefined,
      devices: Array.isArray(body.devices) ? body.devices : [],
    }
  } catch {
    return null
  }
}

/**
 * 踢掉一台已绑定的设备（只动自己账户下的）。
 * 踢的是「当前这台」时网关会直接把我们登出（302 → /login），调用方据此刷新页面。
 */
export async function kickDevice(id: string): Promise<Session | null> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json', 'content-type': 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  const resp = await fetch(withToken('/api/devices/kick', token), {
    method: 'POST',
    headers,
    body: JSON.stringify({ id }),
  })
  if (!resp.ok) {
    const detail = ((await resp.json().catch(() => ({}))) as { error?: string }).error ?? `HTTP ${resp.status}`
    throw new Error(`移除设备失败：${detail}`)
  }
  return fetchSession()
}

export const fetchParams = () => getJSON<Params>('/api/params')
export const fetchHealth = () => getJSON<Health>('/api/health')
export const fetchSummary = (win: TimeWindow) => getJSON<Summary>(`/api/summary?${windowQuery(win)}`)

/**
 * 读数分页。
 *
 * `sample='none'`（默认）＝「最新 limit 条」：窗口比 limit 长时**只给尾部** —— 表格、
 * "最新一条"用它。`sample='auto'` ＝ 窗口行数超过 limit 时对**整个窗口**均匀抽样，
 * 图表用它（否则「选 30 天只画最后几天」，2026-10-09 的故障就是这么来的）。
 */
export const fetchReadings = (win: TimeWindow, limit = 3000, sample: 'none' | 'auto' = 'none') =>
  getJSON<ReadingsPage>(`/api/readings?${windowQuery(win)}&limit=${limit}&sample=${sample}`)

// —— 电磁流量计（FMC400，站号 8）——
//
// 库里只存 14 个原始寄存器（`flow_reading.regs`），单位换算、32 位拼接、空管/反流/报警
// 判定全部在后端（`web/backend/app/flow.py`）。这里只镜像契约 + 格式化，不重算任何口径。

export type FlowStatus = '正常' | '空管' | '反流' | '报警'

export interface FlowReading {
  ts: string
  model: string
  /** 寄存器 0~13 原样（对账用：任何解码都能拿它复核） */
  regs: number[]
  velocity_mps: number
  flow_m3h: number
  percent: number
  empty_pipe_pct: number
  /** 正向累积 m³（32 位会回绕，界面只用它算区间增量） */
  total_m3: number
  reverse_total_m3: number
  /** 报警位原值 */
  alarm: number
  alarm_labels: string[]
  direction: number
  direction_label: string
  status: FlowStatus
}

export interface FlowPage {
  hours: number
  start: string | null
  end: string | null
  limit: number
  offset: number
  count: number
  rows_total: number
  sampled: boolean
  bucket_seconds: number | null
  items: FlowReading[]
}

export interface FlowSummary {
  hours: number
  start: string | null
  end: string | null
  n: number
  n_empty_pipe: number
  n_reverse: number
  n_alarm: number
  flow_avg: number | null
  flow_min: number | null
  flow_max: number | null
  velocity_avg: number | null
  velocity_max: number | null
  empty_pipe_avg: number | null
  /** 满量程占比的区间均值（%）：4~20 mA 那套百分比口径，流量计自己的量程百分比 */
  percent_avg: number | null
  /** 区间首/末累积量（m³）；末值 < 首值（回绕/清零）时 delta 为 null */
  total_first: number | null
  total_last: number | null
  total_delta: number | null
}

export interface FlowLatest {
  model: string
  /** 库里还没有流量行（采集/入库未接入）⇒ false —— 这是正常状态，不是错误 */
  available: boolean
  rows_total: number
  last_ts: string | null
  age_seconds: number | null
  stale: boolean
  reading: FlowReading | null
}

export interface FlowParams {
  model: string
  register_count: number
  units: Record<string, string>
  registers: Array<{ addr: number; len: number; name: string; unit: string; scale: number; signed: boolean }>
  statuses: FlowStatus[]
  alarm_bits: Array<{ mask: string; label: string }>
  stale_seconds: number
  formula: Record<string, string>
  note: string
}

export const fetchFlowParams = () => getJSON<FlowParams>('/api/flow/params')
export const fetchFlowLatest = () => getJSON<FlowLatest>('/api/flow/latest')

/** 语义与 `fetchReadings` 一致：`none`=最新 limit 条，`auto`=整窗均匀抽样（图表用）。 */
export const fetchFlowReadings = (
  win: TimeWindow,
  limit = 3000,
  sample: 'none' | 'auto' = 'none',
) => getJSON<FlowPage>(`/api/flow/readings?${windowQuery(win)}&limit=${limit}&sample=${sample}`)

export const fetchFlowSummary = (win: TimeWindow) =>
  getJSON<FlowSummary>(`/api/flow/summary?${windowQuery(win)}`)

/** 流量计状态 → 颜色档位（浓度计的 `pillClass` 只认四档浓度状态，不能复用）。 */
export function flowPillClass(status: string): string {
  if (status === '正常') return 'ok'
  if (status === '反流') return 'warn'
  if (status === '空管' || status === '报警') return 'bad'
  return 'dim'
}

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
  if (resp.status === 401) await authFailure(resp)
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

/**
 * 导出 Excel 的下载地址：同区间的**浓度结果**，不含仪表原始计数（raw）。
 * 与 CSV 一样只能经查询串带令牌；时间窗与看板完全一致。
 */
export function exportXlsxUrl(win: TimeWindow): string {
  return withToken(`/api/export.xlsx?${windowQuery(win)}`, getToken())
}

/** 流量计导出（CSV）：工程量，不是寄存器 —— 与浓度计分开两个接口。 */
export function exportFlowCsvUrl(win: TimeWindow): string {
  return withToken(`/api/export/flow.csv?${windowQuery(win)}`, getToken())
}

/** 流量计导出（Excel）：列与 CSV 相同（时间/流量/流速/正向累积/流向/状态）。 */
export function exportFlowXlsxUrl(win: TimeWindow): string {
  return withToken(`/api/export/flow.xlsx?${windowQuery(win)}`, getToken())
}

// —— 班次报表（第 5 步）——
//
// 班次边界（白班 08:00–20:00、夜班 20:00–次日 08:00）由**后端**按 `ECAS_TZ` 切，
// 前端只负责选「哪几天、要哪些班」，绝不自己算 12 小时窗口 —— 否则浏览器时区一变，
// 导出的班次就跟看板对不上了。

/**
 * 一个班次。`day` 是班次**开始**那天：「2026-09-25 夜班」= 09-25 20:00 → 09-26 08:00。
 */
export interface ShiftInfo {
  kind: 'day' | 'night'
  kind_label: string
  day: string
  start: string
  end: string
  label: string
  /** 带起止时刻的完整标签，如「2026-09-25 夜班（09-25 20:00 ~ 09-26 08:00）」 */
  label_long: string
  slug: string
  /** 单班导出的文件名（多班时它就是 zip 里的成员名） */
  filename: string
  /** 班次是否已结束；未结束的班统计只覆盖到导出时刻 */
  complete: boolean
}

export interface ShiftsPage {
  count: number
  /** 后端切班次用的时区（`ECAS_TZ`） */
  tz: string
  kinds: string[]
  max_shifts: number
  shifts: ShiftInfo[]
}

/** 班次区间参数：按「天」给，班次边界由后端切。 */
export interface ShiftWindow {
  /** 起始日期 `YYYY-MM-DD`（= 班次开始的那天，含当天） */
  start: string
  /** 结束日期 `YYYY-MM-DD`（含当天） */
  end: string
  /** 要哪些班次：`'day,night'` / `'day'` / `'night'` */
  kinds: string
}

function shiftQuery(win: ShiftWindow): string {
  const p = new URLSearchParams()
  p.set('start', win.start)
  p.set('end', win.end)
  p.set('kinds', win.kinds)
  return p.toString()
}

/**
 * 列出区间内的班次（只算时间，不查数据）——导出前先说清「这次要点出去几份」。
 * 单独走一版而不是 `getJSON`：后端 400 会带一句人话（「班次跨度最多 31 天」），
 * 比 `HTTP 400` 有用得多，要把它透出来。
 */
export async function fetchShifts(win: ShiftWindow): Promise<ShiftsPage> {
  const token = getToken()
  const headers: Record<string, string> = { accept: 'application/json' }
  if (token && asciiOnly(token)) headers.authorization = `Bearer ${token}`
  const resp = await fetch(withToken(`/api/shifts?${shiftQuery(win)}`, token), { headers })
  if (resp.status === 401) throw new AuthError()
  if (!resp.ok) {
    const detail = await resp
      .json()
      .then((body: { detail?: string }) => body.detail)
      .catch(() => undefined)
    throw new Error(detail ?? `班次清单读取失败：HTTP ${resp.status}`)
  }
  return (await resp.json()) as ShiftsPage
}

/**
 * 导出班次 Excel 的下载地址。和 CSV 一样走浏览器原生下载（`<a download>`），
 * 所以**只能**用查询串带令牌。单班 → 直接 `.xlsx`；多班 → 打包成 zip
 * （内含每个班一份 xlsx，外加「说明.txt」做文件↔班次对账）。
 */
export function exportShiftsUrl(win: ShiftWindow, withDetail = true, brief = false): string {
  const extra = `${withDetail ? '' : '&detail=false'}${brief ? '&brief=true' : ''}`
  return withToken(`/api/export/shifts?${shiftQuery(win)}${extra}`, getToken())
}

/** 自定义时段参数：起止都是带时区的 ISO（界面用 `datetime-local`，精确到分钟）。 */
export interface WindowExport {
  start: string
  end: string
}

/**
 * 导出「自定义时段」Excel 的下载地址（先生 2026-11-06：允许导出指定时间点，精确到分钟）。
 *
 * 与班次导出同一套报表结构，只是窗口由用户直接给；浏览器原生下载，令牌只能走查询串。
 */
export function exportWindowUrl(win: WindowExport, withDetail = true, brief = false): string {
  const p = new URLSearchParams()
  p.set('start', win.start)
  p.set('end', win.end)
  if (!withDetail) p.set('detail', 'false')
  if (brief) p.set('brief', 'true')
  return withToken(`/api/export/window?${p.toString()}`, getToken())
}

/** 卡片里的大数字：够短就带千分位，超过 9 个字符就退成科学计数法（如 `1.23e5`）。
 *
 * 先生 2026-11-04：框里的数字变大时会顶出框外 ⇒ 用科学计数法 + 小字号，
 * 而不是把它截断或者让卡片跟着变宽。
 */
export function fitNum(value: number | null | undefined, digits = 0): { text: string; compact: boolean } {
  if (value == null || !Number.isFinite(value)) return { text: '—', compact: false }
  const plain = value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  if (plain.length <= 9) return { text: plain, compact: false }
  // 2 位有效小数足够看趋势；`e+5` 写成 `e5` 更省宽度
  return { text: value.toExponential(2).replace('e+', 'e'), compact: true }
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

/** 矿物真密度 ρp（t/m³）—— 现场给的是固定值 4.3（2026-11-05 先生先改 4.1 后又改回 4.3），不是仪表参数。 */
export const ORE_TRUE_DENSITY = 4.3

/**
 * 干矿量 Q（t）= V / (1/ρp + (1 − C)/C)   —— 先生 2026-11-04 给的现场公式（ρw = 1 已约掉）。
 *
 * V = 矿浆体积（m³，即流量计的流量/累积量）；C = 矿浆**质量浓度**（浓度计读数，本系统按 % 存）。
 * C 换算成 0~1 的小数再代入，且夹到 (0,1) 开区间：C=0 没有矿、C=100% 分母只剩 1/ρp，
 * 两端都是物理边界的极限，直接除会炸。
 */
/**
 * 浓度计与流量计接在**同一根管道**上，流量计在下游约 2~3 m（先生 2026-11-04）。
 * 现场取中值 2.5 m：矿浆从浓度计流到流量计要一段时间 Δt = L / v。
 * v≈3 m/s 时约 0.8 s（可忽略），但低流速 0.2 m/s 时约 12 s —— 低流量工况下不补就会配错点。
 */
export const PIPE_GAP_M = 2.5

/** 传输延时（秒）：Δt = L / v；流速缺失或 ≤0 时返回 null（调用方退化为不补偿）。 */
export function pipeDelaySeconds(velocityMps: number | null | undefined): number | null {
  if (velocityMps == null || !Number.isFinite(velocityMps) || velocityMps <= 0) return null
  const dt = PIPE_GAP_M / velocityMps
  return Number.isFinite(dt) && dt >= 0 ? dt : null
}

export function dryOreMass(
  volumeM3: number | null | undefined,
  concPct: number | null | undefined,
): number | null {
  if (volumeM3 == null || concPct == null) return null
  if (!Number.isFinite(volumeM3) || !Number.isFinite(concPct)) return null
  const c = Math.min(0.999, Math.max(0.001, concPct / 100))
  return volumeM3 / (1 / ORE_TRUE_DENSITY + (1 - c) / c)
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s} 秒`
  if (s < 5400) return `${(s / 60).toFixed(1)} 分钟`
  if (s < 172800) return `${(s / 3600).toFixed(1)} 小时`
  return `${(s / 86400).toFixed(1)} 天`
}
