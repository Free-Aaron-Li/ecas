import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AuthError,
  fetchAuthInfo,
  fetchHealth,
  fetchParams,
  fetchReadings,
  fetchSummary,
  fmtAge,
  getToken,
  setToken as saveToken,
} from './api'
import type { Health, Params, Reading, ReadingsPage, Summary } from './api'
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState(getToken())
  const [authRequired, setAuthRequired] = useState(false)
  const [needToken, setNeedToken] = useState(false)
  const [tokenError, setTokenError] = useState<string | null>(null)
  const { live, connected } = useLiveReading(token)

  // 容器职责：取数与状态，展示交给子组件（presentational）
  const load = useCallback(async (h: number) => {
    try {
      const [readings, sum, hh] = await Promise.all([fetchReadings(h), fetchSummary(h), fetchHealth()])
      setPage(readings)
      setSummary(sum)
      setHealth(hh)
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

      <KpiCards last={last} params={params} />

      <SummaryStrip summary={summary} health={health} />

      <section className="card">
        <div className="chart-head">
          <h2 className="card-title" style={{ margin: 0 }}>
            浓度趋势（断线点不画）
          </h2>
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
        <TrendChart items={chartItems} hours={hours} average={summary?.conc_avg ?? null} loading={loading} />
      </section>

      <section className="card">
        <h2 className="card-title">最近读数（最新 {TABLE_ROWS} 条）</h2>
        <RecentTable rows={rows.slice(0, TABLE_ROWS)} />
      </section>

      <footer className="footer">
        <span>
          口径 <code>{params?.formula.conc ?? 'conc_raw + offset_conc'}</code> ｜ 未补偿{' '}
          <code>{params?.formula.conc_raw ?? '(raw - 4000) * 100 / 16000'}</code>
        </span>
        <span>
          断线判据 <code>raw &lt; {params?.thresholds.break_low ?? 3600}</code> ｜ 超量程 <code>raw &gt; {params?.thresholds.over_high ?? 20500}</code> ｜
          断流阈值 <code>{params?.stale_seconds ?? 600} 秒</code> ｜ 30 秒自动刷新 + WebSocket 实时推送
        </span>
      </footer>
    </div>
  )
}
