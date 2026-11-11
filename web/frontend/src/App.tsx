import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AuthError,
  SessionRevokedError,
  exportCsvUrl,
  exportFlowCsvUrl,
  exportFlowXlsxUrl,
  exportXlsxUrl,
  fmtFull,
  fmtNum,
  fetchAlarms,
  fetchAuthInfo,
  fetchFlowLatest,
  fetchFlowParams,
  fetchFlowReadings,
  fetchFlowSummary,
  fetchHealth,
  fetchParams,
  fetchReadings,
  fetchSession,
  fetchShifts,
  fetchSummary,
  getToken,
  setToken as saveToken,
} from './api'
import type {
  AlarmsPage,
  FlowLatest,
  FlowPage,
  FlowParams,
  FlowSummary,
  Health,
  Params,
  Reading,
  ReadingsPage,
  Session,
  ShiftInfo,
  Summary,
  TimeWindow,
} from './api'
import { AlarmPanel } from './components/AlarmPanel'
import { BrandMark } from './components/BrandMark'
import { FlowPanel } from './components/FlowPanel'
import { ConcentrationPanel } from './components/ConcentrationPanel'
import { ShiftExport } from './components/ShiftExport'
import { InfoPill } from './components/StatusPill'
import { AccountMenu } from './components/AccountMenu'
import { TokenGate } from './components/TokenGate'
import { TopStats } from './components/TopStats'
import { dryOreMass, pipeDelaySeconds } from './api'
import { Math as TeX } from './components/Math'
import { useLiveReading } from './hooks/useLiveReading'

const TABLE_ROWS = 25
/** 趋势图一次要多少点：后端按整窗均匀抽样给到 ≤ 这个数（长区间下"多久一个点"见图上说明） */
const CHART_POINTS = 4000
const POLL_MS = 30_000

// —— 班次（白班 08:00–20:00 / 夜班 20:00–次日 08:00）——
//
// 先生 2026-11-04 定：看板区间按班次走，不再给 1 小时 / 24 小时那种滑动窗口。
// **边界一律以后端 `/api/shifts` 返回的 start/end 为准**（它按 `ECAS_TZ` 切），
// 这里只决定「要哪天、哪个班」——前端绝不自算 12 小时窗口，否则浏览器时区一变就对不上。

type ShiftKind = 'day' | 'night'

/** 选中班次的最小标识：`day` 是班次的**归属日**（= 结束那天；夜班 = 次日早上结束，归次日）。 */
interface ShiftSel {
  day: string
  kind: ShiftKind
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** 本地日期串 `YYYY-MM-DD`。 */
function dayIso(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** 在日期串上前后挪几天（`-1` = 昨天）。 */
function addDays(day: string, delta: number): string {
  const [y, m, d] = day.split('-').map(Number)
  const dt = new Date(y, m - 1, d)
  dt.setDate(dt.getDate() + delta)
  return dayIso(dt)
}

/**
 * 现在正处于哪个班次：08:00–20:00 白班；20:00 之后 / 08:00 之前是夜班。
 * 夜班**归属它结束的那一天**（2026-11-11 先生定）：20:00 起的是「次日」夜班，
 * 凌晨 00:00–08:00 仍在这个「当天」夜班里。
 */
function currentShift(now: Date = new Date()): ShiftSel {
  const today = dayIso(now)
  const h = now.getHours()
  if (h >= 20) return { day: addDays(today, 1), kind: 'night' }
  if (h < 8) return { day: today, kind: 'night' }
  return { day: today, kind: 'day' }
}

/** 当前钟点落在哪种班：08:00–20:00 白班，其余夜班（与日期无关，只决定「本班是白的还是夜的」）。 */
function shiftKindNow(now: Date = new Date()): ShiftKind {
  const h = now.getHours()
  return h >= 20 || h < 8 ? 'night' : 'day'
}

/**
 * 上一个班次（按时间顺序，夜班归属结束日）：
 * - 夜班(D) = D-1 20:00 → D 08:00，紧挨着它之前的是 白班(D-1)；
 * - 白班(D) = D 08:00 → 20:00，紧挨着它之前的是 夜班(D)（即前一晚 20:00 起）。
 */
function prevShift(sel: ShiftSel): ShiftSel {
  if (sel.kind === 'night') return { day: addDays(sel.day, -1), kind: 'day' }
  return { day: sel.day, kind: 'night' }
}

/** 看板上写的区间说明（导出按钮 title / 报警面板 / 统计都用它）。 */
function shiftWindowLabel(shift: ShiftInfo | null): string {
  if (!shift) return '正在确定当前班次…'
  const span = `${fmtFull(shift.start)} → ${fmtFull(shift.end)}`
  return `${shift.label}（${span}${shift.complete ? '' : '，进行中'}）`
}

export default function App() {
  const [params, setParams] = useState<Params | null>(null)
  // 班次：先按本地时间猜「本班」，边界随后端切好再替换；`win` 由 `shift` 推出来（见下）
  const [sel, setSel] = useState<ShiftSel>(() => currentShift())
  const [shift, setShift] = useState<ShiftInfo | null>(null)
  // 每 30 秒推进一次的「现在」：进行中的班次靠它把窗口终点带着走
  const [nowTick, setNowTick] = useState(() => Date.now())
  const [page, setPage] = useState<ReadingsPage | null>(null)
  /** 趋势图专用：整窗均匀抽样（`sample=auto`）。表格/最新值用下面那个精确的小分页。 */
  const [chartPage, setChartPage] = useState<ReadingsPage | null>(null)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  // 「当前区间」与干矿量公式都收在注释里，默认隐藏（先生 2026-11-04）
  const [showNotes, setShowNotes] = useState(false)
  const [alarmsPage, setAlarmsPage] = useState<AlarmsPage | null>(null)
  // 流量计（FMC400）：独立取数、独立空态 —— 表/接口缺失或设备离线都不影响浓度计
  const [flowParams, setFlowParams] = useState<FlowParams | null>(null)
  const [flowLatest, setFlowLatest] = useState<FlowLatest | null>(null)
  const [flowPage, setFlowPage] = useState<FlowPage | null>(null)
  const [flowSummary, setFlowSummary] = useState<FlowSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState(getToken())
  const [authRequired, setAuthRequired] = useState(false)
  const [needToken, setNeedToken] = useState(false)
  const [tokenError, setTokenError] = useState<string | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showAlarms, setShowAlarms] = useState(true)
  const { live, connected } = useLiveReading(token)

  // 班次 → 查询窗口（渲染期推导，不用 effect 回写 state）。
  // 进行中的班：终点跟着 `nowTick` 走；已结束的班：就是整 12 小时。
  const win = useMemo<TimeWindow>(() => {
    if (!shift) return { hours: 12 }
    const startMs = Date.parse(shift.start)
    const endMs = Date.parse(shift.end)
    const end = Math.min(nowTick, endMs)
    const hours = Math.max(0, (end - startMs) / 3_600_000)
    return {
      hours: Math.round(hours * 10) / 10 || 12,
      start: shift.start,
      end: new Date(end).toISOString(),
    }
  }, [shift, nowTick])


  // 选中的班次还没开始（例：14:00 点开「今天夜班」= 今晚 20:00 才起，归明天）：
  // 这时候窗口终点只能取到「现在」，start > end 后端会判非法 400（先生 2026-11-04 撞到的那条红条）。
  // 所以干脆不发请求：面板显示「本班尚未开始」，等到了点再取。
  const notStarted = shift != null && nowTick < Date.parse(shift.start)

  // 容器职责：取数与状态，展示交给子组件（presentational）
  const load = useCallback(async (w: TimeWindow) => {
    try {
      const [chart, readings, sum, hh, alarms] = await Promise.all([
        // 图：整窗抽样（选 30 天也得铺满 30 天）；表：精确的最新 25 条
        fetchReadings(w, CHART_POINTS, 'auto'),
        fetchReadings(w, TABLE_ROWS),
        fetchSummary(w),
        fetchHealth(),
        fetchAlarms(w),
      ])
      setChartPage(chart)
      setPage(readings)
      setSummary(sum)
      setHealth(hh)
      setAlarmsPage(alarms)
      setError(null)
      // 流量计单独取数：`flow_reading` 与 `/api/flow/*` 是 2026-11-03 新增，
      // 库里没表 / 后端还是旧镜像时这里会 404、500 —— 不能把浓度计整页数据一起拖死。
      // 失败就保持 null，区块自己显示「暂未接入数据」。
      void Promise.all([
        fetchFlowParams(),
        fetchFlowLatest(),
        fetchFlowReadings(w, CHART_POINTS, 'auto'),
        fetchFlowSummary(w),
      ])
        .then(([fp, fl, fpg, fs]) => {
          setFlowParams(fp)
          setFlowLatest(fl)
          setFlowPage(fpg)
          setFlowSummary(fs)
        })
        .catch(() => undefined)
    } catch (err) {
      if (err instanceof SessionRevokedError) {
        window.location.href = `/login?m=${encodeURIComponent(err.message)}`
        return
      }
      if (err instanceof AuthError) {
        setNeedToken(true)
        setTokenError(getToken() ? '令牌无效或已过期，请重新输入' : null)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setLoading(false)
    }
  }, [])

  // 网关登录后的提示（`?m=登录成功` / 被强制退出）：显示 6 秒后自动消失，
  // 同时把参数从地址栏抹掉，刷新页面不会又弹一次。
  useEffect(() => {
    const url = new URL(window.location.href)
    const msg = url.searchParams.get('m')
    if (!msg) return
    setNotice(msg)
    url.searchParams.delete('m')
    window.history.replaceState(null, '', url.pathname + (url.search || '') + url.hash)
    const timer = window.setTimeout(() => setNotice(null), 6000)
    return () => window.clearTimeout(timer)
  }, [])

  // 直链带令牌（`http://host/?token=xxx`，隧道/发给自己的链接常用）：
  // 存进 localStorage 后立刻把地址栏里的令牌抹掉（历史记录与复制粘贴都不会漏），
  // 后面的取数就直接带着它了。没带就不动。
  useEffect(() => {
    const url = new URL(window.location.href)
    const fromUrl = url.searchParams.get('token')
    if (!fromUrl) return
    saveToken(fromUrl)
    setToken(fromUrl)
    url.searchParams.delete('token')
    window.history.replaceState(null, '', url.pathname + (url.search || '') + url.hash)
  }, [])

  // 先问后端「要不要令牌」：要，而且本机没有 → 直接弹门（不必等 401）
  useEffect(() => {
    fetchAuthInfo()
      .then((info) => {
        setAuthRequired(info.auth_required)
        if (info.auth_required && !getToken()) setNeedToken(true)
      })
      .catch(() => undefined)
  }, [])

  // 网关会话（部署形态下令牌由网关注入，浏览器只有占位符 ⇒ 界面上不该出现「清除令牌」）
  useEffect(() => {
    let alive = true
    let had = false
    const pull = () =>
      fetchSession()
        .then((next) => {
          if (!alive) return
          if (next) had = true
          // 之前有会话、现在没了 ⇒ 被同一账户在别的设备登录顶掉了（单点在线）
          else if (had) {
            window.location.href = `/login?m=${encodeURIComponent('您的账号已在另一台设备登录，本设备已被强制退出')}`
            return
          }
          setSession(next)
        })
        .catch(() => undefined)
    void pull()
    const timer = window.setInterval(pull, 45_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    if (needToken) return
    fetchParams()
      .then(setParams)
      .catch(() => undefined)
  }, [needToken, token])

  useEffect(() => {
    if (needToken) return
    if (notStarted) {
      // 班次还没开始：清掉上一班的数据，避免图里留着上一次的数字
      setPage(null)
      setChartPage(null)
      setSummary(null)
      setLoading(false)
      return
    }
    setLoading(true)
    void load(win)
  }, [win, load, needToken, notStarted])

  // 选中的班次 → 问后端要边界（只有它能按 `ECAS_TZ` 切班），拿到后写进 `shift`
  useEffect(() => {
    if (needToken) return
    let alive = true
    fetchShifts({ start: sel.day, end: sel.day, kinds: 'day,night' })
      .then((page) => {
        const hit = page.shifts.find((item) => item.kind === sel.kind)
        if (alive && hit) {
          setShift(hit)
          setNowTick(Date.now())
        }
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [sel, needToken])

  // 30 秒一拍：推进「现在」⇒ `win` 变 ⇒ 上面的取数 effect 重新跑（顺带刷新「数据新鲜」）
  useEffect(() => {
    if (needToken) return
    const timer = window.setInterval(() => setNowTick(Date.now()), POLL_MS)
    return () => window.clearInterval(timer)
  }, [needToken])

  const rows = useMemo<Reading[]>(() => {
    const base = page?.items ?? []
    if (live && (!base.length || Date.parse(live.ts) > Date.parse(base[0].ts))) return [live, ...base]
    return base
  }, [page, live])

  const last = rows[0] ?? null


  // 图表用抽样后的整窗数据（时间正序），实时点接在最后 —— 不要拿表格那 25 条画图
  const chartItems = useMemo<Reading[]>(() => {
    const asc = [...(chartPage?.items ?? [])].reverse()
    if (live && (!asc.length || Date.parse(live.ts) > Date.parse(asc[asc.length - 1].ts))) {
      return [...asc, live]
    }
    return asc
  }, [chartPage, live])

  /**
   * 区间干矿量：先生 2026-11-04 要求「综合干矿量取本区间平均干矿量」。
   *
   * 做法是**逐点配对再平均**，不是「平均流量 × 平均浓度」——Q 对 C 是非线性的
   * （Q = V / (1/ρp + (1−C)/C)），拿平均值代进去会系统性偏小。
   *
   * 两支表在同一根管道上、流量计在下游约 2~3 m（先生 2026-11-04）：同一团矿浆先过浓度计、
   * 后到流量计，所以要按**实测流速**把流量点的时间轴往后错 Δt = L / v 再配对
   * （L = PIPE_GAP_M = 2.5 m）。v≈3 m/s 时只差 0.8 s，但低流速 0.2 m/s 时差 12 s，
   * 不补就会把「这一刻的流量」配到「十几秒前的浓度」上。
   * 浓度点与流量点各自按时间升序，给每个浓度点找最近的一个流量点（±90 秒内），
   * 算出一条瞬时 Q，再对这些 Q 取算术平均（等于按时间等权平均，采样本身就是等间隔抽样）。
   */
  // 班次长度（小时）：已结束的班次 = 整班 12 h；进行中的班次 = 开始→此刻。
  // 「累计干矿量」按**每班**累计（先生 2026-11-05），= 本班平均干矿量 × 本班时长。
  const shiftHours = useMemo(() => {
    if (!shift) return 0
    const startMs = Date.parse(shift.start)
    const endMs = Date.parse(shift.end)
    const spanMs = Math.min(nowTick, endMs) - startMs
    return spanMs > 0 ? spanMs / 3_600_000 : 0
  }, [shift, nowTick])

  const oreStats = useMemo(() => {
    const flows = [...(flowPage?.items ?? [])]
      .map((r) => ({ ts: Date.parse(r.ts), flow: r.flow_m3h, v: r.velocity_mps }))
      .sort((a, b) => a.ts - b.ts)
    if (!flows.length) return null
    // 二分找离 targetTs 最近的流量点（容差 tol 毫秒）
    const nearestFlow = (targetTs: number, tol = 90_000) => {
      let lo = 0
      let hi = flows.length - 1
      while (lo < hi) {
        const mid = (lo + hi) >> 1
        if (flows[mid].ts < targetTs) lo = mid + 1
        else hi = mid
      }
      let best: (typeof flows)[number] | null = null
      for (const f of [flows[lo], flows[lo - 1]]) {
        if (!f) continue
        if (!best || Math.abs(f.ts - targetTs) < Math.abs(best.ts - targetTs)) best = f
      }
      return best && Math.abs(best.ts - targetTs) <= tol ? best : null
    }
    const pairs: { ts: number; q: number }[] = []
    for (const c of chartItems) {
      if (c.status !== '正常') continue
      const ts = Date.parse(c.ts)
      // 先按「同一时刻」找流量点，只为取当时的流速来判断传输延时
      const atSameTime = nearestFlow(ts)
      if (!atSameTime) continue
      const delayMs = (pipeDelaySeconds(atSameTime.v) ?? 0) * 1000
      // 矿浆先过浓度计、后到流量计：配对的是「Δt 之后」的流量点；找不到就退回同时刻
      const shifted = delayMs > 0 ? nearestFlow(ts + delayMs) : null
      const best = shifted ?? atSameTime
      const q = dryOreMass(best.flow, c.conc)
      if (q != null) pairs.push({ ts, q })
    }
    if (pairs.length < 2) return null
    pairs.sort((a, b) => a.ts - b.ts)
    const avg = pairs.reduce((a, b) => a + b.q, 0) / pairs.length
    // 累计产量按**本班**累计（先生 2026-11-05）：= 本班平均干矿量 × 本班时长。
    // 不再用梯形积分：断流空洞会让积分只覆盖一小段，算出来的"累计"既不是整班也不可比。
    // 用平均外推的口径与「综合干矿量取区间平均」一致，每个班一个数，班与班可对比。
    const total = shiftHours > 0 ? avg * shiftHours : 0
    return { avg, n: pairs.length, total, hours: shiftHours }
  }, [chartItems, flowPage, shiftHours])
  const stale = health?.stale ?? false

  // 报警面板改了阈值 → 阈值线、越限统计、事件表都得跟着重拉
  const refreshAfterAlarmChange = useCallback(() => {
    void fetchParams()
      .then(setParams)
      .catch(() => undefined)
    void load(win)
  }, [win, load])

  const enterToken = (value: string) => {
    saveToken(value)
    setToken(value)
    setTokenError(null)
    setNeedToken(false)
    setLoading(true)
    void load(win)
  }

  const forgetToken = () => {
    saveToken('')
    setToken('')
    if (authRequired) {
      setNeedToken(true)
      setPage(null)
      setSummary(null)
      setHealth(null)
    }
  }

  // 浓度计区块的工具条：班次快选 / 日期 / 白班夜班 / 两个导出
  // （区间决定导出内容：看的是哪个班，导出的就是这个班）
  const windowLabel = shiftWindowLabel(shift)
  // 班次选择器：浓度计与流量计共用一块（看的是同一个区间），导出各用各的接口
  const shiftPicker = (
    <>
      {/* 先生 2026-11-04：日期排最前，后面跟「本班/上一班」这种快捷选择 */}
      <label className="shift-day" title="班次所属日（夜班 = 次日早上结束的那天，归该日）">
        <input
          type="date"
          value={sel.day}
          max={dayIso(new Date())}
          onChange={(e) => {
            const v = e.target.value
            if (v) setSel({ day: v, kind: sel.kind })
          }}
        />
      </label>
      <div className="tabs" role="group" aria-label="班次快选">
        {/* 先生 2026-11-04：本班/上一班按**所选日期**算 —— 选了 11/03，本班就是 11/03 的那个班，
            不是现实今天的班。班次种类仍按当前钟点（08–20 白班，其余夜班）。 */}
        <button type="button" onClick={() => setSel({ day: sel.day, kind: shiftKindNow() })}>
          本班
        </button>
        <button
          type="button"
          onClick={() => setSel(prevShift({ day: sel.day, kind: shiftKindNow() }))}
        >
          上一班
        </button>
      </div>
      <div className="tabs" role="group" aria-label="白班 / 夜班">
        {(['day', 'night'] as const).map((kind) => (
          <button
            key={kind}
            type="button"
            aria-pressed={sel.kind === kind}
            onClick={() => setSel({ day: sel.day, kind })}
          >
            {kind === 'day' ? '白班' : '夜班'}
          </button>
        ))}
      </div>
    </>
  )

  // 浓度计的两个导出（CSV 含原始计数 / Excel 只留浓度）；流量计那边用它自己的两个接口
  const concExports = (
    <>
      {/* 班次还没开始 ⇒ 区间是空的（start > end），导出按钮先不给，免得点出个 400 */}
      {!notStarted && (
        <>
          <a
            className="header-btn link"
            href={exportCsvUrl(win)}
            download
            title={`导出 ${windowLabel} 的读数（CSV，含原始计数）`}
          >
            导出 CSV
          </a>
          <a
            className="header-btn link"
            href={exportXlsxUrl(win)}
            download
            title={`导出 ${windowLabel} 的浓度数据（Excel，不含原始计数）`}
          >
            导出 Excel
          </a>
        </>
      )}
    </>
  )

  // 浓度计卡片用的完整工具条 = 班次选择器 + 浓度导出
  const shiftToolbar = (
    <>
      {shiftPicker}
      {concExports}
    </>
  )

  const header = (
    <header className="header">
      <div className="brand">
        <BrandMark />
        <h1>浓度监测</h1>
      </div>
      <div className="header-right">
        {alarmsPage && alarmsPage.active > 0 && (
          <InfoPill tone="warn" text={`进行中报警 ${alarmsPage.active}`} />
        )}
        <InfoPill tone={connected ? 'ok' : 'dim'} text={connected ? '实时推送已连接' : '实时推送未连接'} />
        {session ? (
          // 账户网关形态：姓名/用户名/登录设备都在下拉里（先生 2026-11-04）
          <>
            {session.role === 'admin' && (
              <a className="header-btn link" href="/admin">
                账户管理
              </a>
            )}
            <AccountMenu session={session} />
          </>
        ) : (
          // 直连后端形态（本机 5173 / 隧道）：令牌在 localStorage，保留手工清除
          authRequired &&
          token && (
            <button type="button" className="header-btn" onClick={forgetToken}>
              清除令牌
            </button>
          )
        )}
        {/* 数据新鲜度只在「综合采集状态」出一次（先生 2026-11-04），页头不再重复 */}
      </div>
    </header>
  )

  if (needToken) {
    return (
      <div className="app">
        {header}
        <TokenGate onSubmit={enterToken} error={tokenError} />
        <footer className="footer">
          <span>
            探针 <code>/healthz</code> 与值守门状态 <code>/api/auth</code> 无需令牌，其余接口都需要
          </span>
        </footer>
      </div>
    )
  }

  return (
    <div className="app">
      {header}

      {notice && (
        <div className="toast" role="status">
          <span>{notice}</span>
          <button type="button" className="toast-close" onClick={() => setNotice(null)} aria-label="关闭提示">
            ×
          </button>
        </div>
      )}

      {error && (
        <div className="card error">
          读取后端失败：{error} —— 请确认后端在 <code>127.0.0.1:8080</code> 上运行（<code>uv run uvicorn app.main:app</code>）。
        </div>
      )}

      {/* 最顶层：综合/瞬时干矿量（按现场公式现算）+ 综合采集状态 */}
      <TopStats
        health={health}
        stale={stale}
        connected={connected}
        concStatus={last?.status ?? null}
        flowStatus={flowLatest?.reading?.status ?? null}
        avgOre={oreStats?.avg ?? null}
        avgPoints={oreStats?.n ?? 0}
        totalOre={oreStats ? oreStats.total : null}
        shiftHours={oreStats?.hours ?? 0}
        flowNow={flowLatest?.reading?.flow_m3h ?? null}
        concNow={last?.conc ?? null}
      />

      {/* 注释默认收起：里面放「当前区间」+ 干矿量公式与口径（先生 2026-11-04） */}
      <div className="notes">
        <button
          type="button"
          className="header-btn link notes-toggle"
          aria-expanded={showNotes}
          onClick={() => setShowNotes((v) => !v)}
        >
          {showNotes ? '收起注释' : '注释'}
        </button>
        {showNotes && (
          <div className="notes-body">
            <p>
              <b>当前区间</b>：{windowLabel}
              {notStarted ? '（本班尚未开始）' : ''}
            </p>
            <div className="notes-formula">
              <TeX tex={String.raw`Q = \dfrac{V}{\dfrac{1}{\rho_p} + \dfrac{1 - C}{C}}`} display />
              <p>
                其中：<TeX tex={String.raw`\rho_w = 1`} /> t/m³（水的密度，已在推导中约掉）；
                <TeX tex={String.raw`\rho_p = 3.44`} /> t/m³（铁精矿真密度，现场固定值）；
                <TeX tex="C" /> 为矿浆质量浓度（浓度计读数，按 % 计）；
                <TeX tex="V" /> 为矿浆体积（流量计给出的 m³）；<TeX tex="Q" /> 为干矿量（t）。
              </p>
              <p className="notes-dim">
                推导：<TeX tex={String.raw`V = \dfrac{Q}{\rho_p} + \dfrac{W}{\rho_w}`} />，
                <TeX tex={String.raw`C = \dfrac{Q}{Q + W}`} /> ⇒{" "}
                <TeX tex={String.raw`W = Q\cdot\dfrac{1 - C}{C}`} />；
                取 <TeX tex={String.raw`\rho_w = 1`} /> 代入即得上式。
              </p>
            </div>
            <p>
              <b>瞬时干矿量</b>：<TeX tex="V" /> 取当前流量（m³/h）、<TeX tex="C" /> 取当前浓度 ⇒ t/h，
              浓度一变它立刻跟着变。
            </p>
            <p>
              <b>综合干矿量</b>（t/h，本区间平均）＝ 把区间内每个浓度点与<b>错开传输延时之后</b>的流量点
              （容差 ±90 秒）配对，逐点算 <TeX tex="Q" /> 之后再取算术平均。之所以不写「平均流量 ×
              平均浓度」：<TeX tex="Q" /> 对 <TeX tex="C" /> 是非线性的，先平均再相乘会系统性偏小。
            </p>
            <p>
              <b>两表位置补偿</b>：浓度计与流量计接在<b>同一根管道</b>上，流量计在下游约 2~3 m（本系统按
              中值 <TeX tex="L = 2.5" /> m 计）。同一团矿浆先过浓度计、后到流量计，间隔为
              <TeX tex={String.raw`\Delta t = \dfrac{L}{v}`} />（<TeX tex="v" /> 取该时刻流量计的实测流速）。
              <TeX tex="v\approx 3" /> m/s 时 <TeX tex="\Delta t\approx 0.8" /> s 可忽略；低流速
              <TeX tex="0.2" /> m/s 时约 12 s，不补就会把「此刻的流量」配到「十几秒前的浓度」上。
              因此配对时把流量点的时间轴按 <TeX tex="\Delta t" /> 往后错，图上两条曲线仍各按自己的
              时间轴绘制（不做右移显示）。
            </p>
            <p>
              <b>区间怎么划</b>：一天两个班 —— 白班 <TeX tex={String.raw`08{:}00 \to 20{:}00`} />，
              夜班 <TeX tex={String.raw`20{:}00 \to \text{次日 }08{:}00`} />（时区 Asia/Shanghai，按服务器时区）。
              页头/卡片上的「本班 / 上一班 / 班次日期 / 白班·夜班」选哪一段，上面所有统计、趋势、导出
              就都按那一段算；正在进行的班次取「从开始到此刻」，还没开始的班次不取数（避免把未来的空档算进来）。
            </p>
            <p>
              <b>本班累计 ≈ 是什么</b>（t）＝ <b>按每班累计</b>：
              <TeX tex={String.raw`Q_{\text{班}} = \bar{Q} \times \Delta H_{\text{班}}`} />，
              即「本班平均干矿量 <TeX tex="\bar{Q}" />（t/h）」×「本班时长
              <TeX tex="\Delta H_{\text{班}}" />（h）」。已结束的班次按整班 12 h 计，
              进行中的班次按「开始 → 此刻」计。这样每个班一个数、班与班之间可直接比较；
              断流空洞不参与平均（不会被算进均值），也不再用「积分覆盖时长」这种说法。
            </p>
            <p className="notes-dim">
              注：浓度只取「正常」状态的点；断线与超量程属设备级异常，其浓度是外推值，不参与干矿量计算。
              流量与浓度来自两块独立仪表，配对容差 ±90 秒内才算同一时刻；流量计在下游 2~3 m，
              已按实测流速补偿传输延时。
            </p>
          </div>
        )}
      </div>

      {/* 浓度计：当前浓度 / 区间统计 / 趋势 / 逐条读数，全在一个框里（版式对齐下面的流量计） */}
      <ConcentrationPanel
        last={last}
        chartItems={chartItems}
        summary={summary}
        params={params}
        alarms={params?.alarms}
        loading={loading}
        hours={win.hours}
        windowStart={chartPage?.start ?? null}
        windowEnd={chartPage?.end ?? null}
        sampled={chartPage?.sampled ?? false}
        rowsTotal={chartPage?.rows_total ?? 0}
        bucketSeconds={chartPage?.bucket_seconds ?? null}
        showAlarms={showAlarms}
        toolbar={shiftToolbar}
        notStarted={notStarted}
        onToggleAlarms={() => setShowAlarms((v) => !v)}
      />

      <FlowPanel
        params={flowParams}
        latest={flowLatest}
        page={flowPage}
        summary={flowSummary}
        loading={loading}
        windowStart={flowPage?.start ?? null}
        windowEnd={flowPage?.end ?? null}
        hours={win.hours}
        windowLabel={windowLabel}
        toolbar={shiftPicker}
        exportCsvHref={notStarted ? undefined : exportFlowCsvUrl(win)}
        exportXlsxHref={notStarted ? undefined : exportFlowXlsxUrl(win)}
      />

      <AlarmPanel
        alarms={params?.alarms}
        page={alarmsPage}
        windowLabel={windowLabel}
        onChanged={refreshAfterAlarmChange}
      />


      <ShiftExport />

      <footer className="footer">
        <span>
          报警阈值 <code>浓度 &lt; {fmtNum(params?.alarms.low, 1)}</code> /{' '}
          <code>浓度 &gt; {fmtNum(params?.alarms.high, 1)}</code>
          {params?.alarms.custom ? '（自定义）' : '（默认）'} ｜ 断流阈值{' '}
          <code>{params?.stale_seconds ?? 600} 秒</code>
        </span>
        <span>30 秒自动刷新 + WebSocket 实时推送</span>
      </footer>
    </div>
  )
}
