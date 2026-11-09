import type { ReactNode } from 'react'
import { useRef, useState } from 'react'
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
import type { Alarms, Reading } from '../api'
import { alarmSide, fmtDateTime, fmtNum } from '../api'
import { ZoomTouch } from './ZoomTouch'

interface Point {
  iso: string
  ts: number
  conc: number
  raw: number
  ma: number
  status: string
}

function ChartTooltip({
  active,
  payload,
  alarms,
}: {
  active?: boolean
  payload?: Array<{ payload: Point }>
  alarms?: Alarms | null
}) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  const side = alarmSide(p.conc, alarms)
  return (
    <div className="tooltip">
      <div className="t">
        {fmtDateTime(p.iso)} ｜ {p.status}
        {side && <span className={side === 'high' ? 'alarm-high' : 'alarm-low'}> ｜ 越限</span>}
      </div>
      <div className="row">
        <span className="k">浓度</span>
        <span>
          {fmtNum(p.conc, 3)}
          <span className="unit"> %</span>
        </span>
      </div>
    </div>
  )
}

/** 一个点代表多长时间（抽样时提示用）：秒 → 人话。 */
function fmtBucket(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} 秒`
  if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟`
  if (seconds < 36 * 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${(seconds / 86400).toFixed(1)} 天`
}

/** 浓度趋势（面积图）。数据按时间正序；**断线点不画**（它是故障标记而非测量值）。 */
export function TrendChart({
  items,
  hours,
  average,
  loading,
  alarms,
  showAlarms = true,
  windowStart,
  windowEnd,
  sampled = false,
  rowsTotal = 0,
  toolbar,
  title,
  bucketSeconds = null,
}: {
  items: Reading[]
  hours: number
  average: number | null
  loading: boolean
  /** 报警上下限（浓度）——由后端 `/api/params` 给出，前端不重算 */
  alarms?: Alarms | null
  showAlarms?: boolean
  toolbar?: ReactNode
  /** 图表标题（与工具条同一行，右侧对齐） */
  title?: ReactNode
  /** 所选区间的起止（ISO）：X 轴按它铺满，而不是按"手里这点数据"铺满 */
  windowStart?: string | null
  windowEnd?: string | null
  /** 后端做了整窗抽样（`sample=auto`）：要在图上说明"抽了多少、多久一个点" */
  sampled?: boolean
  /** 窗口内的总测点数（抽样前） */
  rowsTotal?: number
  /** 抽样时一个点代表多少秒 */
  bucketSeconds?: number | null
}) {
  // 放大缩小（先生 2026-11-04：浓度趋势要和流量趋势一样能定起止）：null = 看整段
  const [zoom, setZoom] = useState<{ start: number; end: number } | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)

  const data: Point[] = items
    .filter((r) => r.status !== '断线') // 断线点不是测量值，画进曲线会把 Y 轴拉到负区间
    .map((r) => ({
      iso: r.ts,
      ts: Date.parse(r.ts),
      conc: r.conc,
      raw: r.raw,
      ma: r.ma,
      status: r.status,
    }))

  if (loading && !data.length) {
    return <div className="skeleton">正在加载趋势数据…</div>
  }
  if (!data.length) {
    return <div className="skeleton">该区间没有数据。</div>
  }

  // X 轴固定到**所选区间**（数据比窗口窄也不会缩成"最近几天"；数据越界时再放宽，
  // 免得刚推来的实时点被切掉）。2026-10-09 之前这里用 dataMin/dataMax，配上被
  // LIMIT 截断的数据就是"选 30 天只画 4 天"的观感。
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

  // Brush 按数组下标工作：把时间窗口折成下标（数据严格按时间升序）
  const startIdx = Math.max(0, data.findIndex((p) => p.ts >= domainStart))
  const endIdx = Math.max(startIdx, data.reduce((acc, p, i) => (p.ts <= domainEnd ? i : acc), 0))

  const localInput = (ms: number) => {
    const d = new Date(ms)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const setEdges = (start: number, end: number) => {
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return
    setZoom({
      start: base ? Math.max(base[0], start) : start,
      end: base ? Math.min(base[1], end) : end,
    })
  }

  // X 轴刻度**必须显式给**：同一时间戳在库里可能有多条行（采集端补传，PK 是 (model, ts, raw)），
  // Recharts 会按数据点逐个生成刻度 —— 2026-11-04 实测生成了 813 个一模一样的「11:11」，
  // 全部叠在同一点，远看就是一条灰带横在 X 轴上方（先生问「趋势图下面那行是什么」）。
  // 均分 6 段 ⇒ 只有 7 个刻度，既读得出时间跨度，也不会再叠成一团。
  const ticks =
    Number.isFinite(domainStart) && Number.isFinite(domainEnd) && domainEnd > domainStart
      ? Array.from({ length: 7 }, (_, i) => Math.round(domainStart + ((domainEnd - domainStart) * i) / 6))
      : undefined

  const tick = (value: number) => {
    const d = new Date(value)
    const hh = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    return hours <= 24 ? hh : `${d.getMonth() + 1}/${d.getDate()} ${hh}`
  }

  return (
    <>
      {sampled && (
        <div className="chart-note">
          该区间共 {rowsTotal.toLocaleString()} 个测点，图上按时间**均匀抽样**为 {data.length} 个点
          {bucketSeconds ? `（约每 ${fmtBucket(bucketSeconds)} 一个）` : ''}：每个点都是库里一条真实读数
          （不插值、不平均），但比这更短的峰谷可能被抽掉 —— 要逐秒看请缩短区间。
        </div>
      )}
      <div className="chart-head conc-chart-head">
        <h2 className="card-title" style={{ margin: 0 }}>
          {title}
        </h2>
        <div className="chart-actions">
          <div className="chart-tools">
            {toolbar}
            {toolbar ? <span className="tool-divider" aria-hidden /> : null}
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
      </div>
      <div className="chart-box conc-chart-box" ref={boxRef}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 18, bottom: 0, left: -6 }}>
            <defs>
              <linearGradient id="concFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#22c55e" stopOpacity={0.34} />
                <stop offset="100%" stopColor="#22c55e" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              scale="time"
              domain={xDomain}
              tickFormatter={tick}
              ticks={ticks}
              tick={{ fill: '#64748b', fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: '#334155' }}
            />
            <YAxis
              tick={{ fill: '#64748b', fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: '#334155' }}
              width={46}
              domain={['auto', 'auto']}
              tickFormatter={(v: number) => v.toFixed(1)}
            />
            <Tooltip content={<ChartTooltip alarms={alarms} />} cursor={{ stroke: '#475569', strokeDasharray: '3 3' }} />
            {showAlarms && alarms && (
              <>
                <ReferenceLine
                  y={alarms.high}
                  ifOverflow="extendDomain"
                  stroke="#f59e0b"
                  strokeDasharray="5 3"
                  label={{
                    value: `上限 ${fmtNum(alarms.high, 1)}`,
                    fill: '#f59e0b',
                    fontSize: 11,
                    position: 'insideTopRight',
                  }}
                />
                <ReferenceLine
                  y={alarms.low}
                  ifOverflow="extendDomain"
                  stroke="#ef4444"
                  strokeDasharray="5 3"
                  label={{
                    value: `下限 ${fmtNum(alarms.low, 1)}`,
                    fill: '#ef4444',
                    fontSize: 11,
                    position: 'insideBottomRight',
                  }}
                />
              </>
            )}
            {average != null && (
              <ReferenceLine
                y={average}
                stroke="#475569"
                strokeDasharray="4 4"
                label={{ value: `均值 ${fmtNum(average, 2)}`, fill: '#94a3b8', fontSize: 11, position: 'insideTopRight' }}
              />
            )}
            <Area
              type="monotone"
              dataKey="conc"
              stroke="#22c55e"
              strokeWidth={2}
              fill="url(#concFill)"
              dot={false}
              activeDot={{ r: 4, fill: '#22c55e', stroke: '#0f172a', strokeWidth: 2 }}
              isAnimationActive={false}
              name="浓度"
            />
            {/* 拖两端 = 框选放大；拖中间 = 平移；拖回全宽自动还原 */}
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
