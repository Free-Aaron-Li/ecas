import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AuthError,
  concFormula,
  exportCsvUrl,
  fetchAlarms,
  fetchAuthInfo,
  fetchHealth,
  fetchParams,
  fetchReadings,
  fetchSummary,
  fmtAge,
  fmtNum,
  getToken,
  setToken as saveToken,
} from './api'
import type { AlarmsPage, Health, Params, Reading, ReadingsPage, Summary } from './api'
import { AlarmPanel } from './components/AlarmPanel'
import { KpiCards } from './components/KpiCards'
import { RecentTable } from './components/RecentTable'
import { InfoPill } from './components/StatusPill'
import { SummaryStrip } from './components/SummaryStrip'
import { TokenGate } from './components/TokenGate'
import { TrendChart } from './components/TrendChart'
import { useLiveReading } from './hooks/useLiveReading'

const RANGES = [
  { hours: 1, label: '1 小时' },
  { hours: 24, label: '24 小时' },
  { hours: 168, label: '7 天' },
  { hours: 720, label: '30 天' },
]

const TABLE_ROWS = 25
const POLL_MS = 30_000

export default function App() {
  const [params, setParams] = useState<Params | null>(null)
  const [hours, setHours] = useState(24)
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
  const [showAlarms, setShowAlarms] = useState(true)
  const { live, connected } = useLiveReading(token)

  // 容器职责：取数与状态，展示交给子组件（presentational）
  const load = useCallback(async (h: number) => {
    try {
      const [readings, sum, hh, alarms] = await Promise.all([
        fetchReadings(h),
        fetchSummary(h),
        fetchHealth(),
        fetchAlarms(h),
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

  useEffect(() => {
    if (needToken) return
    fetchParams()
      .then(setParams)
      .catch(() => undefined)
  }, [needToken, token])

  useEffect(() => {
    if (needToken) return
    setLoading(true)
    void load(hours)
  }, [hours, load, needToken])

  useEffect(() => {
    if (needToken) return
    const timer = window.setInterval(() => void load(hours), POLL_MS)
    return () => window.clearInterval(timer)
  }, [hours, load, needToken])

  const rows = useMemo<Reading[]>(() => {
    const base = page?.items ?? []
    if (live && (!base.length || Date.parse(live.ts) > Date.parse(base[0].ts))) return [live, ...base]
    return base
  }, [page, live])

  const last = rows[0] ?? null
  const chartItems = useMemo(() => [...rows].reverse(), [rows])
  const stale = health?.stale ?? false

  // 报警面板改了阈值 → 阈值线、越限统计、事件表都得跟着重拉
  const refreshAfterAlarmChange = useCallback(() => {
    void fetchParams()
      .then(setParams)
      .catch(() => undefined)
    void load(hours)
  }, [hours, load])

  const enterToken = (value: string) => {
    saveToken(value)
    setToken(value)
    setTokenError(null)
    setNeedToken(false)
    setLoading(true)
    void load(hours)
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
        {authRequired && token && (
          <button type="button" className="header-btn" onClick={forgetToken}>
            清除令牌
          </button>
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

      <SummaryStrip summary={summary} health={health} alarms={params?.alarms} />

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
            <a className="header-btn link" href={exportCsvUrl(hours)} download>
              导出 CSV（{hours} 小时）
            </a>
            <div className="tabs" role="group" aria-label="时间区间">
              {RANGES.map((r) => (
                <button
                  key={r.hours}
                  type="button"
                  aria-pressed={hours === r.hours}
                  onClick={() => setHours(r.hours)}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <TrendChart
          items={chartItems}
          hours={hours}
          average={summary?.conc_avg ?? null}
          loading={loading}
          alarms={params?.alarms}
          showAlarms={showAlarms}
        />
      </section>

      <AlarmPanel alarms={params?.alarms} page={alarmsPage} hours={hours} onChanged={refreshAfterAlarmChange} />

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
