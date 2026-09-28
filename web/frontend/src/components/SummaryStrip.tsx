import type { Alarms, Health, Summary } from '../api'
import { fmtNum } from '../api'

/** 区间内的浓度变化（首→末，只算有效测量点）。 */
export interface ConcChange {
  from: number
  to: number
  delta: number
}

interface Cell {
  /** 指标名（小字标签） */
  k: string
  /** 指标值（等宽数字） */
  v: string
  /** 单位（更小的灰字，紧跟数值；单独元素 ⇒ 中英文混排不打架） */
  u?: string
  /** 指标下的补充说明（更小的灰字） */
  sub?: string
  /** hot = 异常计数非零；up/down = 区间变化的方向 */
  tone?: 'hot' | 'up' | 'down'
}

/** 把「滞后秒数」拆成数值 + 单位，让所有瓦片结构一致。 */
function ageParts(seconds: number | null | undefined): { v: string; u?: string } {
  if (seconds === null || seconds === undefined) return { v: '—' }
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return { v: String(s), u: '秒' }
  if (s < 5400) return { v: `${(s / 60).toFixed(1)}`, u: '分钟' }
  if (s < 172800) return { v: `${(s / 3600).toFixed(1)}`, u: '小时' }
  return { v: `${(s / 86400).toFixed(1)}`, u: '天' }
}

/**
 * 区间统计条：按「数据量 / 浓度 / 越限与异常」三行堆叠，每行左侧是固定宽度的行标题，
 * 右侧是一排**等宽瓦片**——三行走的是同一张网格，所以任何一行的瓦片都严丝合缝对齐。
 *
 * 均值、极值只统计有效区间（后端已排除断线行）；越限数用同一套后端阈值，
 * 所以这里的数字与图表上的报警线、报警事件表永远一致。
 */
export function SummaryStrip({
  summary,
  health,
  alarms,
  windowLabel,
  change,
}: {
  summary: Summary | null
  health: Health | null
  alarms?: Alarms | null
  windowLabel: string
  change?: ConcChange | null
}) {
  const count = (n: number | undefined) => (summary ? String(n ?? 0) : '—')
  const tone = (n: number | undefined): Cell['tone'] => (n ? 'hot' : undefined)
  const age = ageParts(health?.age_seconds)
  const dropped = summary ? Math.max(0, summary.n - summary.n_valid) : 0

  const groups: Array<{ title: string; cells: Cell[] }> = [
    {
      title: '数据量',
      cells: [
        {
          k: '有效样本',
          v: summary ? String(summary.n_valid) : '—',
          sub: dropped ? `已排除 ${dropped}` : undefined,
        },
        { k: '库存总量', v: health ? String(health.rows_total) : '—', u: '行' },
        { k: '原始最低', v: summary?.raw_min != null ? String(summary.raw_min) : '—' },
        { k: '原始最高', v: summary?.raw_max != null ? String(summary.raw_max) : '—' },
        { k: '最新滞后', v: age.v, u: age.u },
      ],
    },
    {
      title: '浓度',
      cells: [
        { k: '平均', v: fmtNum(summary?.conc_avg, 3) },
        { k: '最低', v: fmtNum(summary?.conc_min, 2) },
        { k: '最高', v: fmtNum(summary?.conc_max, 2) },
        {
          k: '区间变化',
          v: change ? `${change.delta > 0 ? '+' : ''}${change.delta.toFixed(3)}` : '—',
          sub: change ? `${change.from.toFixed(1)} → ${change.to.toFixed(1)}` : undefined,
          tone: change ? (change.delta > 0 ? 'up' : change.delta < 0 ? 'down' : undefined) : undefined,
        },
      ],
    },
    {
      title: '越限与异常',
      cells: [
        { k: '断线', v: count(summary?.broken), tone: tone(summary?.broken) },
        { k: '偏低', v: count(summary?.low), tone: tone(summary?.low) },
        { k: '超量程', v: count(summary?.over), tone: tone(summary?.over) },
        { k: '低于下限', v: count(summary?.n_below_alarm), tone: tone(summary?.n_below_alarm) },
        { k: '高于上限', v: count(summary?.n_above_alarm), tone: tone(summary?.n_above_alarm) },
      ],
    },
  ]

  return (
    <div className="card strip">
      <div className="strip-head">
        <span className="win">
          区间 <b>{windowLabel}</b>
        </span>
        <span className="thr">
          报警阈值 {fmtNum(alarms?.low, 1)} ~ {fmtNum(alarms?.high, 1)}
        </span>
      </div>
      {groups.map((g) => (
        <div className="strip-sec" key={g.title}>
          <div className="sec-name">{g.title}</div>
          <div className="tiles">
            {g.cells.map((c) => (
              <div
                key={c.k}
                className={`tile${c.tone ? ` ${c.tone}` : ''}`}
                title={c.sub ? `${c.k}：${c.v}${c.u ?? ''}（${c.sub}）` : `${c.k}：${c.v}${c.u ?? ''}`}
              >
                <span className="t-k">{c.k}</span>
                <span className="t-v">
                  {c.v}
                  {c.u && <span className="t-u">{c.u}</span>}
                </span>
                {c.sub && <span className="t-sub">{c.sub}</span>}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
