import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { Reading } from '../api'
import { fmtDateTime, fmtNum } from '../api'

interface Point {
  iso: string
  ts: number
  conc: number
  raw: number
  ma: number
  status: string
}

function ChartTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: Point }> }) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload
  return (
    <div className="tooltip">
      <div className="t">
        {fmtDateTime(p.iso)} ｜ {p.status}
      </div>
      <div className="row">
        <span className="k">浓度</span>
        <span>{fmtNum(p.conc, 3)}</span>
      </div>
      <div className="row">
        <span className="k">电流</span>
        <span>{fmtNum(p.ma, 3)} mA</span>
      </div>
      <div className="row">
        <span className="k">原始值</span>
        <span>{p.raw}</span>
      </div>
    </div>
  )
}

/** 浓度趋势（面积图）。数据按时间正序；**断线点不画**（它是故障标记而非测量值）。 */
export function TrendChart({
  items,
  hours,
  average,
  loading,
}: {
  items: Reading[]
  hours: number
  average: number | null
  loading: boolean
}) {
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

  const tick = (value: number) => {
    const d = new Date(value)
    const hh = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    return hours <= 24 ? hh : `${d.getMonth() + 1}/${d.getDate()} ${hh}`
  }

  return (
    <div className="chart-box">
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
            domain={['dataMin', 'dataMax']}
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
            width={46}
            domain={['auto', 'auto']}
            tickFormatter={(v: number) => v.toFixed(1)}
          />
          <Tooltip content={<ChartTooltip />} cursor={{ stroke: '#475569', strokeDasharray: '3 3' }} />
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
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
