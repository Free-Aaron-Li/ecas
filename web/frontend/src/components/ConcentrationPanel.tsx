import { useMemo } from 'react'
import type { ReactNode } from 'react'
import type { Alarms, Params, Reading, Summary } from '../api'
import { alarmSide, fitNum, fmtDateTime, fmtNum, trimNum } from '../api'
import { TrendChart } from './TrendChart'

/** 最近 1 小时的浓度变化（首→末，只算有效测量点）。 */
export interface ConcChange {
  from: number
  to: number
  delta: number
}

/**
 * 浓度计区块：当前浓度 / 区间统计 / 趋势，**全部收在同一个框里**。
 *
 * 2026-11-04 先生定：
 * * 只展示浓度，不摆电流、原始计数（那是仪表内部量）；
 * * 版式对齐下面的流量计区块：KPI → 统计瓦片 → 趋势图；
 * * **不要「最近读数」表**（逐条读数已删，趋势图足够看，明细去导 Excel）。
 */
export function ConcentrationPanel({
  last,
  chartItems,
  summary,
  params,
  alarms,
  loading,
  hours,
  windowStart,
  windowEnd,
  sampled,
  rowsTotal,
  bucketSeconds,
  showAlarms,
  toolbar,
  notStarted = false,
  onToggleAlarms,
}: {
  last: Reading | null
  chartItems: Reading[]
  summary: Summary | null
  params: Params | null
  alarms?: Alarms | null
  loading: boolean
  hours: number
  windowStart: string | null
  windowEnd: string | null
  sampled: boolean
  rowsTotal: number
  bucketSeconds: number | null
  showAlarms: boolean
  /** 班次选择与导出按钮：由 App 拼好塞进来，面板本身不认识路由参数 */
  toolbar: ReactNode
  /** 选中的班次还没开始（例如下午点开当天的夜班）—— 不发请求，也不摆空图 */
  notStarted?: boolean
  onToggleAlarms: () => void
}) {
  const offsetText =
    params?.offset_mode === 'mul'
      ? `×${(1 + (params?.offset_pct ?? 0) / 100).toFixed(4)}`
      : `+${trimNum(params?.offset_conc ?? 0)}`

  // 越限只在「正常」档才有意义（断线/超量程是设备级异常，那条浓度是外推值）
  const side = last && last.status === '正常' ? alarmSide(last.conc, alarms) : null
  const dropped = summary ? Math.max(0, summary.n - summary.n_valid) : 0
  const tone = (n: number | undefined) => (n ? ' hot' : '')
  // 区间变化**单独定为最近 1 小时**（先生 2026-11-04），不再拿整个班次首尾相减 —— 班里一断流，
  // 首尾两个点可能隔着几小时，差值就没意义了。只算有效测量点（断线是故障标记，不是测量值）。
  const change = useMemo<ConcChange | null>(() => {
    const pts = chartItems.filter((r) => r.status !== '断线')
    if (pts.length < 2) return null
    const lastTs = Date.parse(pts[pts.length - 1].ts)
    const hourAgo = pts.filter((r) => Date.parse(r.ts) >= lastTs - 3_600_000)
    const base = hourAgo.length >= 2 ? hourAgo[0] : pts[pts.length - 2]
    const to = pts[pts.length - 1]
    return { from: base.conc, to: to.conc, delta: to.conc - base.conc }
  }, [chartItems])
  const changeTone = change ? (change.delta >= 0 ? ' up' : ' down') : ''

  return (
    <section className="card conc">
      <div className="chart-head">
        <h2 className="card-title" style={{ margin: 0 }}>
          浓度计{params?.model ? `（${params.model}）` : ''}
        </h2>
        <div className="chart-actions">{toolbar}</div>
      </div>

      <div className="kpis conc-kpis">
        <div className="card kpi primary">
          <span className="label">当前浓度</span>
          {notStarted ? (
            <span className="skeleton">本班尚未开始</span>
          ) : last ? (
            <>
              <span className="value">
                {fmtNum(last.conc, 3)}
                <span className="unit">%</span>
              </span>
              <span className="sub">
                补偿 {offsetText} ｜ 数据时刻 {fmtDateTime(last.ts)}
              </span>
              {side && (
                <span className={side === 'high' ? 'alarm-tag high' : 'alarm-tag low'}>
                  {side === 'high'
                    ? `高于报警上限 ${fmtNum(alarms?.high ?? 0, 1)}`
                    : `低于报警下限 ${fmtNum(alarms?.low ?? 0, 1)}`}
                </span>
              )}
            </>
          ) : (
            <span className="skeleton">正在读取数据…</span>
          )}
        </div>

      </div>

      <div className="tiles">
        <div className="tile">
          <span className="t-k">有效样本</span>
          <span className={`t-v${fitNum(summary?.n_valid ?? null, 0).compact ? ' compact' : ''}`}>
            {fitNum(summary?.n_valid ?? null, 0).text}
          </span>
          <span className="t-sub">{dropped ? `已排除 ${dropped}` : '条'}</span>
        </div>
        <div className="tile">
          <span className="t-k">平均</span>
          <span className="t-v">{fmtNum(summary?.conc_avg ?? null, 2)}</span>
          <span className="t-u">%</span>
        </div>
        <div className="tile">
          <span className="t-k">最低</span>
          <span className="t-v">{fmtNum(summary?.conc_min ?? null, 2)}</span>
          <span className="t-u">%</span>
        </div>
        <div className="tile">
          <span className="t-k">最高</span>
          <span className="t-v">{fmtNum(summary?.conc_max ?? null, 2)}</span>
          <span className="t-u">%</span>
        </div>
        <div className={`tile${changeTone}`}>
          <span className="t-k">1 小时变化</span>
          <span className="t-v">{change ? `${change.delta >= 0 ? '+' : ''}${fmtNum(change.delta, 3)}` : '—'}</span>
          <span className="t-sub">
            {change ? `${fmtNum(change.from, 2)} → ${fmtNum(change.to, 2)}` : '首→末'}
          </span>
        </div>
        <div className={`tile${tone(summary?.n_below_alarm)}`}>
          <span className="t-k">低于下限</span>
          <span className="t-v">{summary ? summary.n_below_alarm : '—'}</span>
          <span className="t-sub">次</span>
        </div>
        <div className={`tile${tone(summary?.n_above_alarm)}`}>
          <span className="t-k">高于上限</span>
          <span className="t-v">{summary ? summary.n_above_alarm : '—'}</span>
          <span className="t-sub">次</span>
        </div>
        <div className={`tile${tone(summary?.broken)}`}>
          <span className="t-k">断线</span>
          <span className="t-v">{summary ? summary.broken : '—'}</span>
          <span className="t-sub">次</span>
        </div>
        <div className={`tile${tone(summary?.over)}`}>
          <span className="t-k">超量程</span>
          <span className="t-v">{summary ? summary.over : '—'}</span>
          <span className="t-sub">次</span>
        </div>
      </div>

      <TrendChart
        title={
          <>
            浓度趋势<span className="title-unit">（%）</span>
          </>
        }
        items={chartItems}
        hours={hours}
        average={summary?.conc_avg ?? null}
        loading={loading}
        alarms={alarms}
        showAlarms={showAlarms}
        windowStart={windowStart}
        windowEnd={windowEnd}
        sampled={sampled}
        rowsTotal={rowsTotal}
        bucketSeconds={bucketSeconds}
        toolbar={
          <button
            type="button"
            className="header-btn"
            aria-pressed={showAlarms}
            onClick={onToggleAlarms}
            disabled={!params}
          >
            报警线 {showAlarms ? '开' : '关'}
          </button>
        }
      />
    </section>
  )
}
