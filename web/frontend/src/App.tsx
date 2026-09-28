import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AuthError,
  concFormula,
  exportCsvUrl,
  fmtAge,
  fmtFull,
  fmtNum,
  fromLocalInput,
  fetchAlarms,
  fetchAuthInfo,
  fetchHealth,
  fetchParams,
  fetchReadings,
  fetchSession,
  fetchSummary,
  getToken,
  setToken as saveToken,
  toLocalInput,
} from './api'
import type {
  AlarmsPage,
  Health,
  Params,
  Reading,
  ReadingsPage,
  Session,
  Summary,
  TimeWindow,
} from './api'
import { AlarmPanel } from './components/AlarmPanel'
import { KpiCards } from './components/KpiCards'
import { RecentTable } from './components/RecentTable'
import { InfoPill } from './components/StatusPill'
import { SummaryStrip } from './components/SummaryStrip'
import { TokenGate } from './components/TokenGate'
import { TrendChart } from './components/TrendChart'
import { useLiveReading } from './hooks/useLiveReading'

/** 预设区间：只给 hours ⇒ 终点＝现在，窗口随时间滑动。 */
const PRESETS: TimeWindow[] = [
  { hours: 1, label: '1 小时' },
  { hours: 24, label: '24 小时' },
  { hours: 168, label: '7 天' },
  { hours: 720, label: '30 天' },
]
const DEFAULT_WIN = PRESETS[1]

/** 自定义时段上限：与后端 `MAX_WINDOW_HOURS` 一致（超了后端报 400，前端先说清楚）。 */
const MAX_CUSTOM_HOURS = 720

const TABLE_ROWS = 25
const POLL_MS = 30_000

function hoursAgoIso(hours: number): string {
  return new Date(Date.now() - hours * 3_600_000).toISOString()
}

/** 看板上写的时段说明（导出按钮的 title、统计条的「区间」都用它）。 */
function windowText(win: TimeWindow): string {
  if (win.start && win.end) return `${fmtFull(win.start)} → ${fmtFull(win.end)}（${win.hours} 小时）`
  return `最近 ${win.hours} 小时（滑动窗口）`
}

export default function App() {
  const [params, setParams] = useState<Params | null>(null)
  const [win, setWin] = useState<TimeWindow>(DEFAULT_WIN)
  const [page, setPage] = useState<ReadingsPage | null>(null)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [alarmsPage, setAlarmsPage] = useState<AlarmsPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState(getToken())
  const [authRequired, setAuthRequired] = useState(false)
  const [needToken, setNeedToken] = useState(false)
  const [tokenError, setTokenError] = useState<string | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [showAlarms, setShowAlarms] = useState(true)
  // 自定义时段输入框（本地时间串，按「应用」才生效）
  const [rangeOpen, setRangeOpen] = useState(false)
  const [rangeStart, setRangeStart] = useState(() => toLocalInput(hoursAgoIso(6)))
  const [rangeEnd, setRangeEnd] = useState(() => toLocalInput(new Date().toISOString()))
  const [rangeError, setRangeError] = useState<string | null>(null)
  const { live, connected } = useLiveReading(token)

  // 容器职责：取数与状态，展示交给子组件（presentational）
  const load = useCallback(async (w: TimeWindow) => {
    try {
      const [readings, sum, hh, alarms] = await Promise.all([
        fetchReadings(w),
        fetchSummary(w),
        fetchHealth(),
        fetchAlarms(w),
      ])
      setPage(readings)
      setSummary(sum)
      setHealth(hh)
      setAlarmsPage(alarms)
      setError(null)
    } catch (err) {
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
    fetchSession()
      .then(setSession)
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    if (needToken) return
    fetchParams()
      .then(setParams)
      .catch(() => undefined)
  }, [needToken, token])

  useEffect(() => {
    if (needToken) return
    setLoading(true)
    void load(win)
  }, [win, load, needToken])

  useEffect(() => {
    if (needToken) return
    const timer = window.setInterval(() => void load(win), POLL_MS)
    return () => window.clearInterval(timer)
  }, [win, load, needToken])

  const rows = useMemo<Reading[]>(() => {
    const base = page?.items ?? []
    if (live && (!base.length || Date.parse(live.ts) > Date.parse(base[0].ts))) return [live, ...base]
    return base
  }, [page, live])

  const last = rows[0] ?? null
  const chartItems = useMemo(() => [...rows].reverse(), [rows])
  const stale = health?.stale ?? false

  // 区间内的浓度变化（首→末，只算有效测量点；断线是故障标记不是测量值）
  const change = useMemo(() => {
    const pts = chartItems.filter((r) => r.status !== '断线')
    if (pts.length < 2) return null
    const from = pts[0].conc
    const to = pts[pts.length - 1].conc
    return { from, to, delta: to - from }
  }, [chartItems])

  // 报警面板改了阈值 → 阈值线、越限统计、事件表都得跟着重拉
  const refreshAfterAlarmChange = useCallback(() => {
    void fetchParams()
      .then(setParams)
      .catch(() => undefined)
    void load(win)
  }, [win, load])

  const applyRange = () => {
    const start = fromLocalInput(rangeStart)
    const end = fromLocalInput(rangeEnd)
    if (!start || !end) {
      setRangeError('起止时间都得填')
      return
    }
    const spanH = (Date.parse(end) - Date.parse(start)) / 3_600_000
    if (spanH <= 0) {
      setRangeError('起点必须早于终点')
      return
    }
    if (spanH > MAX_CUSTOM_HOURS) {
      setRangeError(`最长 ${MAX_CUSTOM_HOURS / 24} 天（当前 ${(spanH / 24).toFixed(1)} 天）`)
      return
    }
    setRangeError(null)
    setWin({ hours: Math.max(1, Math.round(spanH)), start, end })
    setRangeOpen(false)
  }

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

  const header = (
    <header className="header">
      <div>
        <h1>ECAS · 浓度计实时监测</h1>
        <div className="sub">
          {params?.model ?? 'DPRO-66'} ｜ 塔石 DTU → MQTT → PostgreSQL ｜{' '}
          {params ? `${params.range.i_lo}~${params.range.i_hi} mA ↔ ${params.range.c_lo}~${params.range.c_hi}` : '4~20 mA ↔ 0~100'}
        </div>
      </div>
      <div className="header-right">
        {alarmsPage && alarmsPage.active > 0 && (
          <InfoPill tone="warn" text={`进行中报警 ${alarmsPage.active}`} />
        )}
        <InfoPill tone={connected ? 'ok' : 'dim'} text={connected ? '实时推送已连接' : '实时推送未连接'} />
        {session ? (
          // 账户网关形态：令牌由网关注入，浏览器只有占位符 ⇒ 这里只给「退出登录」
          <>
            {session.role === 'admin' && (
              <a className="header-btn link" href="/admin">
                账户管理
              </a>
            )}
            <span className="header-user" title={`登录账户 ${session.user}（${session.role || 'viewer'}）`}>
              {session.user}
            </span>
            <form className="logout-form" method="post" action="/logout">
              <button type="submit" className="header-btn">
                退出登录
              </button>
            </form>
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
        {health ? (
          <InfoPill
            tone={stale ? 'warn' : 'ok'}
            text={stale ? `无新数据 ${fmtAge(health.age_seconds)}` : `数据新鲜（滞后 ${fmtAge(health.age_seconds)}）`}
          />
        ) : (
          <InfoPill tone="dim" text="尚未取到数据" />
        )}
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

      {error && (
        <div className="card error">
          读取后端失败：{error} —— 请确认后端在 <code>127.0.0.1:8080</code> 上运行（<code>uv run uvicorn app.main:app</code>）。
        </div>
      )}

      <KpiCards last={last} params={params} alarms={params?.alarms} />

      <SummaryStrip
        summary={summary}
        health={health}
        alarms={params?.alarms}
        windowLabel={win.start && win.end ? `${fmtFull(win.start)} → ${fmtFull(win.end)}` : `${win.hours} 小时`}
        change={change}
      />

      <section className="card">
        <div className="chart-head">
          <h2 className="card-title" style={{ margin: 0 }}>
            浓度趋势（断线点不画）
          </h2>
          <div className="chart-actions">
            <button
              type="button"
              className="header-btn"
              aria-pressed={showAlarms}
              onClick={() => setShowAlarms((v) => !v)}
              disabled={!params}
            >
              报警线 {showAlarms ? '开' : '关'}
            </button>
            <a
              className="header-btn link"
              href={exportCsvUrl(win)}
              download
              title={`导出 ${windowText(win)} 的原始值 + 换算结果（CSV）`}
            >
              导出 CSV（{win.start ? '自定义时段' : win.label}）
            </a>
            <div className="tabs" role="group" aria-label="时间区间">
              {PRESETS.map((r) => (
                <button
                  key={r.hours}
                  type="button"
                  aria-pressed={!win.start && win.hours === r.hours}
                  onClick={() => setWin(r)}
                >
                  {r.label}
                </button>
              ))}
              <button
                type="button"
                aria-pressed={!!win.start}
                onClick={() => setRangeOpen((v) => !v)}
                title="自定义起止时间点"
              >
                自定义
              </button>
            </div>
          </div>
        </div>

        <div className="range-bar">
          <span className="range-now">当前区间：{windowText(win)}</span>
          {win.start && (
            <button type="button" className="range-clear" onClick={() => setWin(DEFAULT_WIN)}>
              回到最近 {DEFAULT_WIN.label}
            </button>
          )}
        </div>

        {rangeOpen && (
          <div className="range-panel">
            <label>
              起点
              <input
                type="datetime-local"
                value={rangeStart}
                onChange={(e) => setRangeStart(e.target.value)}
              />
            </label>
            <label>
              终点
              <input type="datetime-local" value={rangeEnd} onChange={(e) => setRangeEnd(e.target.value)} />
            </label>
            <div className="range-quick">
              {[1, 6, 24, 168].map((h) => (
                <button
                  key={h}
                  type="button"
                  onClick={() => {
                    setRangeEnd(toLocalInput(new Date().toISOString()))
                    setRangeStart(toLocalInput(hoursAgoIso(h)))
                    setRangeError(null)
                  }}
                >
                  近 {h < 24 ? `${h} 小时` : `${h / 24} 天`}
                </button>
              ))}
            </div>
            <div className="range-actions">
              <button type="button" className="header-btn primary" onClick={applyRange}>
                应用
              </button>
              <button type="button" className="header-btn" onClick={() => setRangeOpen(false)}>
                取消
              </button>
            </div>
            {rangeError && <span className="range-err">{rangeError}</span>}
          </div>
        )}

        <TrendChart
          items={chartItems}
          hours={win.hours}
          average={summary?.conc_avg ?? null}
          loading={loading}
          alarms={params?.alarms}
          showAlarms={showAlarms}
        />
      </section>

      <AlarmPanel
        alarms={params?.alarms}
        page={alarmsPage}
        windowLabel={win.start ? '该自定义时段' : `近 ${win.hours} 小时`}
        onChanged={refreshAfterAlarmChange}
      />

      <section className="card">
        <div className="chart-head">
          <h2 className="card-title" style={{ margin: 0 }}>
            最近读数（最新 {TABLE_ROWS} 条）
          </h2>
          <span className="hint">
            越限判据：{fmtNum(params?.alarms.low, 1)} ~ {fmtNum(params?.alarms.high, 1)} 浓度
            （raw {params?.alarms.low_raw ?? '—'} ~ {params?.alarms.high_raw ?? '—'}）
          </span>
        </div>
        <RecentTable rows={rows.slice(0, TABLE_ROWS)} alarms={params?.alarms} />
      </section>

      <footer className="footer">
        <span>
          口径 <code>{concFormula(params)}</code> ｜ 未补偿{' '}
          <code>{params?.formula.conc_raw ?? '(raw - 4000) * 100 / 16000'}</code>
        </span>
        <span>
          报警 <code>浓度 &lt; {fmtNum(params?.alarms.low, 1)}</code> /{' '}
          <code>浓度 &gt; {fmtNum(params?.alarms.high, 1)}</code>
          {params?.alarms.custom ? '（看板自定义）' : '（出厂默认，可在看板改）'} ｜ 断线判据{' '}
          <code>raw &lt; {params?.thresholds.break_low ?? 3600}</code> ｜ 超量程{' '}
          <code>raw &gt; {params?.thresholds.over_high ?? 20500}</code> ｜ 断流阈值{' '}
          <code>{params?.stale_seconds ?? 600} 秒</code> ｜ 30 秒自动刷新 + WebSocket 实时推送
        </span>
      </footer>
    </div>
  )
}
