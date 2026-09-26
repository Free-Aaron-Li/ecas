import type { Alarms, Health, Summary } from '../api'
import { fmtAge, fmtNum } from '../api'

/** 区间统计条：均值/极值只统计有效区间（后端已排除断线行）。 */
export function SummaryStrip({
  summary,
  health,
  alarms,
}: {
  summary: Summary | null
  health: Health | null
  alarms?: Alarms | null
}) {
  const exceed = summary ? summary.n_below_alarm + summary.n_above_alarm : null
  const cells: Array<[string, string]> = [
    ['区间', summary ? `${summary.hours} 小时` : '—'],
    ['有效样本', summary ? `${summary.n_valid} / ${summary.n}` : '—'],
    ['平均浓度', fmtNum(summary?.conc_avg, 3)],
    ['最小 / 最大', `${fmtNum(summary?.conc_min, 2)} / ${fmtNum(summary?.conc_max, 2)}`],
    ['原始值范围', summary?.raw_min != null ? `${summary.raw_min} ~ ${summary.raw_max}` : '—'],
    ['异常行', summary ? `断线 ${summary.broken} ｜ 偏低 ${summary.low} ｜ 超量程 ${summary.over}` : '—'],
    [
      `越限（${fmtNum(alarms?.low, 1)}~${fmtNum(alarms?.high, 1)}）`,
      summary ? `低于下限 ${summary.n_below_alarm} ｜ 高于上限 ${summary.n_above_alarm}` : '—',
    ],
    ['库存总量', health ? `${health.rows_total} 行` : '—'],
    ['最新数据滞后', fmtAge(health?.age_seconds)],
  ]

  return (
    <div className="card strip">
      {cells.map(([k, v]) => (
        <div className={k.startsWith('越限') && exceed ? 'cell hot' : 'cell'} key={k}>
          <span className="k">{k}</span>
          <span className="v">{v}</span>
        </div>
      ))}
    </div>
  )
}
