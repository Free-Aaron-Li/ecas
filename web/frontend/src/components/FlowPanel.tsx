import {
  Area,
  AreaChart,
  Brush,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { FlowLatest, FlowPage, FlowParams, FlowSummary } from '../api'
import { fitNum, fmtDateTime, fmtNum } from '../api'
import { ZoomTouch } from './ZoomTouch'

interface Point {
  iso: string
  ts: number
  flow: number
  velocity: number
  total: number
  status: string
}

function FlowTooltip({
  active,
  payload,
  units,
}: {
  active?: boolean
  payload?: Array<{ payload: Point }>
  units?: Record<string, string> | null
}) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  return (
    <div className="tooltip">
      <div className="t">
        {fmtDateTime(p.iso)} ｜ {p.status}
      </div>
      <div className="row">
        <span className="k">瞬时流量</span>
        <span>
          {fmtNum(p.flow, 2)} {units?.flow ?? 'm³/h'}
        </span>
      </div>
      <div className="row">
        <span className="k">流速</span>
        <span>
          {fmtNum(p.velocity, 3)} {units?.velocity ?? 'm/s'}
        </span>
      </div>
      <div className="row">
        <span className="k">正向累积</span>
        <span>
          {p.total.toLocaleString()} {units?.total ?? 'm³'}
        </span>
      </div>
    </div>
  )
}

/** 空态：库里还没有流量行 —— 只说「现在没有」，不写采集链路细节。 */
function EmptyFlow({ rowsTotal }: { rowsTotal: number }) {
  return (
    <div className="flow-empty">
      <div className="flow-empty-title">暂未接入流量数据</div>
      <p>
        {rowsTotal > 0
          ? `流量计已有 ${rowsTotal.toLocaleString()} 条读数，但所选区间没有数据。`
          : '还没有流量计（FMC400）读数。'}
      </p>
      <p className="flow-empty-hint">流量计接入后，本区块会在 30 秒内自动出现，无需刷新页面。</p>
    </div>
  )
}

/**
 * 电磁流量计（FMC400）区块。
 *
 * 与浓度计共用页面时间窗（预设 / 自定义都跟随），但**独立取数、独立空态**：
 * 流量计 2026-11-03 才接入采集，设备离线或入库未跑时浓度计照常显示，
 * 这里明确写「暂未接入数据」而不是让整页报错。
 */
export function FlowPanel({
  params,
  latest,
  page,
  summary,
  loading,
  windowStart,
  windowEnd,
  hours,
  windowLabel,
  toolbar,
  exportCsvHref,
  exportXlsxHref,
}: {
  params: FlowParams | null
  latest: FlowLatest | null
  page: FlowPage | null
  summary: FlowSummary | null
  loading: boolean
  windowStart: string | null
  windowEnd: string | null
  hours: number
  /** 与浓度计同一个班次说明（两块看的是同一个区间） */
  windowLabel: string
  /** 班次选择（本班/上一班/日期/白班夜班）：由 App 拼好，面板不认识路由 */
  toolbar?: ReactNode
  /** 流量导出的下载地址（同区间，CSV 带注释头、Excel 带标题区） */
  exportCsvHref?: string
  exportXlsxHref?: string
}) {
  const reading = latest?.reading ?? null
  const units = params?.units ?? null
  const data: Point[] = [...(page?.items ?? [])]
    .reverse()
    .map((r) => ({
      iso: r.ts,
      ts: Date.parse(r.ts),
      flow: r.flow_m3h,
      velocity: r.velocity_mps,
      total: r.total_m3,
      status: r.status,
    }))

  const head = (
    <div className="chart-head">
      <h2 className="card-title" style={{ margin: 0 }}>
        电磁流量计（FMC400）
      </h2>
      <div className="chart-actions">
        {toolbar}
        {exportCsvHref && (
          <a className="header-btn link" href={exportCsvHref} download title={`导出 ${windowLabel} 的流量数据（CSV）`}>
            导出 CSV
          </a>
        )}
        {exportXlsxHref && (
          <a className="header-btn link" href={exportXlsxHref} download title={`导出 ${windowLabel} 的流量数据（Excel）`}>
            导出 Excel
          </a>
        )}
        {/* 新鲜度只在顶部「综合采集状态」出一次（先生 2026-11-04），这里不再挂 pill */}
      </div>
    </div>
  )

  if (!latest?.available) {
    return (
      <section className="card flow">
        {head}
        <EmptyFlow rowsTotal={latest?.rows_total ?? 0} />
      </section>
    )
  }

  return (
    <section className="card flow">
      {head}

      {/* 即时读数：一行看全「此刻」——流量、流速、满量程、仪表累积、流向、状态 */}
      <div className="kpis flow-kpis">
        <div className="card kpi primary">
          <span className="label">当前流量</span>
          <span className={`value${fitNum(reading?.flow_m3h ?? null, 2).compact ? ' compact' : ''}`}>
            {fitNum(reading?.flow_m3h ?? null, 2).text}
            <span className="unit">{units?.flow ?? 'm³/h'}</span>
          </span>
          {/* 流速与量程占比下面已有专门的卡（先生 2026-11-04：不要在这里重复），只留数据时刻 */}
          <span className="sub">{reading ? `数据时刻 ${fmtDateTime(reading.ts)}` : '暂无读数'}</span>
        </div>

        <div className="card kpi">
          <span className="label">流速</span>
          <span className="value">
            {fmtNum(reading?.velocity_mps ?? null, 3)}
            <span className="unit">{units?.velocity ?? 'm/s'}</span>
          </span>
          <span className="sub">
            区间最高 {fmtNum(summary?.velocity_max ?? null, 3)} {units?.velocity ?? 'm/s'}
          </span>
        </div>

        <div className="card kpi">
          <span className="label">满量程占比</span>
          <span className="value">
            {fmtNum(reading?.percent ?? null, 1)}
            <span className="unit">%</span>
          </span>
          <span className="sub">区间均值 {fmtNum(summary?.percent_avg ?? null, 1)}%</span>
        </div>

        <div className="card kpi">
          <span className="label">正向累积（仪表）</span>
          <span className={`value${fitNum(reading?.total_m3 ?? null, 0).compact ? ' compact' : ''}`}>
            {fitNum(reading?.total_m3 ?? null, 0).text}
            <span className="unit">{units?.total ?? 'm³'}</span>
          </span>
          <span className="sub">
            {summary?.total_delta != null
              ? `本区间 +${summary.total_delta.toLocaleString()} ${units?.total ?? 'm³'}`
              : '回绕或清零时不给区间增量'}
          </span>
        </div>

        <div className="card kpi">
          <span className="label">流向</span>
          <span className="value">{reading ? reading.direction_label : '—'}</span>
          <span className="sub">仪表判定</span>
        </div>

      </div>

      {/* 区间统计：与浓度计统计条同一种瓦片网格，宽度对齐 */}
      <div className="flow-tiles">
        <div className="tiles">
          <div className="tile">
            <span className="t-k">区间均值</span>
            <span className="t-v">{fmtNum(summary?.flow_avg ?? null, 2)}</span>
            <span className="t-u">{units?.flow ?? 'm³/h'}</span>
          </div>
          <div className="tile">
            <span className="t-k">最高</span>
            <span className="t-v">{fmtNum(summary?.flow_max ?? null, 2)}</span>
            <span className="t-u">{units?.flow ?? 'm³/h'}</span>
          </div>
          <div className="tile">
            <span className="t-k">最低</span>
            <span className="t-v">{fmtNum(summary?.flow_min ?? null, 2)}</span>
            <span className="t-u">{units?.flow ?? 'm³/h'}</span>
          </div>
          <div className="tile">
            <span className="t-k">流速均值</span>
            <span className="t-v">{fmtNum(summary?.velocity_avg ?? null, 3)}</span>
            <span className="t-u">m/s</span>
          </div>
          <div className="tile">
            <span className="t-k">测点</span>
            <span className={`t-v${fitNum(summary?.n ?? 0, 0).compact ? ' compact' : ''}`}>
              {fitNum(summary?.n ?? 0, 0).text}
            </span>
            <span className="t-sub">行</span>
          </div>
          <div className={`tile${summary?.n_empty_pipe ? ' hot' : ''}`}>
            <span className="t-k">空管</span>
            <span className="t-v">{summary?.n_empty_pipe ?? 0}</span>
            <span className="t-sub">次</span>
          </div>
          <div className={`tile${summary?.n_reverse ? ' hot' : ''}`}>
            <span className="t-k">反流</span>
            <span className="t-v">{summary?.n_reverse ?? 0}</span>
            <span className="t-sub">次</span>
          </div>
          <div className={`tile${summary?.n_alarm ? ' hot' : ''}`}>
            <span className="t-k">报警</span>
            <span className="t-v">{summary?.n_alarm ?? 0}</span>
            <span className="t-sub">次</span>
          </div>
          <div className="tile">
            <span className="t-k">区间增量</span>
            <span className={`t-v${fitNum(summary?.total_delta ?? null, 0).compact ? ' compact' : ''}`}>
              {fitNum(summary?.total_delta ?? null, 0).text}
            </span>
            <span className="t-u">{units?.total ?? 'm³'}</span>
          </div>
        </div>
      </div>

      {loading && !data.length ? (
        <div className="skeleton">正在加载流量数据…</div>
      ) : !data.length ? (
        <div className="skeleton">该区间没有流量数据。</div>
      ) : (
        <FlowChart data={data} hours={hours} windowStart={windowStart} windowEnd={windowEnd} units={units} />
      )}
    </section>
  )
}

function FlowChart({
  data,
  hours,
  windowStart,
  windowEnd,
  units,
}: {
  data: Point[]
  hours: number
  windowStart: string | null
  windowEnd: string | null
  units: Record<string, string> | null
}) {
  // 放大缩小：null = 看整个班次；用户拖 Brush 或改起止时间后 = 只看这一段（先生 2026-11-04）
  const [zoom, setZoom] = useState<{ start: number; end: number } | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)

  // X 轴固定到所选区间（与浓度趋势图同一处理）：数据比窗口窄也不会缩成"最近一段"
  let lo = windowStart ? Date.parse(windowStart) : Number.NaN
  let hi = windowEnd ? Date.parse(windowEnd) : Number.NaN
  for (const p of data) {
    if (!Number.isFinite(lo) || p.ts < lo) lo = p.ts
    if (!Number.isFinite(hi) || p.ts > hi) hi = p.ts
  }
  const base: [number, number] | null = Number.isFinite(lo) && Number.isFinite(hi) && hi > lo ? [lo, hi] : null
  const domainStart = zoom ? Math.max(zoom.start, base?.[0] ?? zoom.start) : (base?.[0] ?? Number.NaN)
  const domainEnd = zoom ? Math.min(zoom.end, base?.[1] ?? zoom.end) : (base?.[1] ?? Number.NaN)
  const xDomain: [number, number] | ['dataMin', 'dataMax'] =
    Number.isFinite(domainStart) && Number.isFinite(domainEnd) && domainEnd > domainStart
      ? [domainStart, domainEnd]
      : ['dataMin', 'dataMax']

  // Brush 是按**数组下标**工作的，这里把时间窗口折成下标（数据严格按时间升序）
  const startIdx = Math.max(0, data.findIndex((p) => p.ts >= domainStart))
  const endIdx = Math.max(startIdx, data.reduce((acc, p, i) => (p.ts <= domainEnd ? i : acc), 0))

  const localInput = (ms: number) => {
    const d = new Date(ms)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const setEdges = (start: number, end: number) => {
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return
    const lo2 = base ? Math.max(base[0], start) : start
    const hi2 = base ? Math.min(base[1], end) : end
    setZoom({ start: lo2, end: hi2 })
  }

  const tick = (value: number) => {
    const d = new Date(value)
    const hh = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    return hours <= 24 ? hh : `${d.getMonth() + 1}/${d.getDate()} ${hh}`
  }

  return (
    <>
      <div className="chart-head flow-chart-head">
        <h2 className="card-title" style={{ margin: 0 }}>
          流量趋势<span className="title-unit">（m³/h）</span>
        </h2>
        <div className="chart-actions">
          <label className="zoom-field" title="放大到从这里开始">
            <span>起始</span>
            <input
              type="datetime-local"
              value={Number.isFinite(domainStart) ? localInput(domainStart) : ''}
              min={base ? localInput(base[0]) : undefined}
              max={Number.isFinite(domainEnd) ? localInput(domainEnd) : undefined}
              onChange={(e) => {
                const v = Date.parse(e.target.value)
                if (Number.isFinite(v)) setEdges(v, Number.isFinite(domainEnd) ? domainEnd : v + 3_600_000)
              }}
            />
          </label>
          <label className="zoom-field" title="放大到到这里结束">
            <span>结束</span>
            <input
              type="datetime-local"
              value={Number.isFinite(domainEnd) ? localInput(domainEnd) : ''}
              min={Number.isFinite(domainStart) ? localInput(domainStart) : undefined}
              max={base ? localInput(base[1]) : undefined}
              onChange={(e) => {
                const v = Date.parse(e.target.value)
                if (Number.isFinite(v)) setEdges(Number.isFinite(domainStart) ? domainStart : v - 3_600_000, v)
              }}
            />
          </label>
          <button type="button" className="header-btn" disabled={!zoom} onClick={() => setZoom(null)}>
            重置
          </button>
        </div>
      </div>

      <div className="chart-box flow-chart-box" ref={boxRef}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 18, bottom: 0, left: -6 }}>
            <defs>
              <linearGradient id="flowFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.34} />
                <stop offset="100%" stopColor="#38bdf8" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              scale="time"
              domain={xDomain}
              tickFormatter={tick}
              tick={{ fill: '#64748b', fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: '#334155' }}
              minTickGap={44}
            />
            <YAxis
              tick={{ fill: '#64748b', fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: '#334155' }}
              width={52}
              domain={['auto', 'auto']}
              tickFormatter={(v: number) => v.toFixed(1)}
            />
            {/* 0 线：反流时曲线会落到下面，画一条参考线比让读者自己猜正负更清楚 */}
            <ReferenceLine y={0} stroke="#475569" strokeDasharray="2 3" />
            <Tooltip content={<FlowTooltip units={units} />} cursor={{ stroke: '#475569', strokeDasharray: '3 3' }} />
            <Area
              type="monotone"
              dataKey="flow"
              stroke="#38bdf8"
              strokeWidth={2}
              fill="url(#flowFill)"
              dot={false}
              activeDot={{ r: 4, fill: '#38bdf8', stroke: '#0f172a', strokeWidth: 2 }}
              isAnimationActive={false}
              name="瞬时流量"
            />
            {/* 拖两端 = 框选放大；拖中间 = 平移到别的时段 */}
            <Brush
              dataKey="ts"
              height={24}
              travellerWidth={8}
              stroke="#475569"
              fill="#0b1220"
              startIndex={startIdx}
              endIndex={endIdx}
              tickFormatter={tick}
              onChange={(r) => {
                const si = r.startIndex ?? 0
                const ei = r.endIndex ?? data.length - 1
                if (!data[si] || !data[ei]) return
                if (si <= 0 && ei >= data.length - 1) setZoom(null)
                else setZoom({ start: data[si].ts, end: data[ei].ts })
              }}
            />
          </AreaChart>
        </ResponsiveContainer>
        <ZoomTouch
          boxRef={boxRef}
          base={base}
          domainStart={domainStart}
          domainEnd={domainEnd}
          onEdges={setEdges}
        />
      </div>
    </>
  )
}
