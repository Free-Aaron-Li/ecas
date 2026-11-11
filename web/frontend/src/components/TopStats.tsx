import type { Health } from '../api'
import { dryOreMass, fitNum, fmtAge, fmtNum } from '../api'

/**
 * 最顶层三项经营统计：综合干矿量 / 瞬时干矿量 / 综合采集状态。
 *
 * 前两项按现场公式现算（先生 2026-11-04 给的口径）：
 *   Q = V / (1/ρp + (1−C)/C)，ρp = 3.44 t/m³（铁精矿真密度），C = 浓度计读数，V = 矿浆体积。
 *   - 瞬时干矿量：V = **当前**流量（m³/h）、C = **当前**浓度 ⇒ t/h，浓度一变它就跟着变。
 *   - 综合干矿量：本区间内**逐点配对算完再取平均**（不是平均流量×平均浓度，Q 对 C 非线性），t/h。
 * 缺任一路数据就显示 `—`：宁可空着，也不能出现一个看着像真实产量、其实是 0 的假数。
 *
 * 第三项「综合采集状态」是**唯一**的数据新鲜度/链路状况出口（先生 2026-11-04：
 * 「数据新鲜度综合到综合采集状态，其他地方不要显示」）——页头与流量卡头都不再单独挂 pill。
 */
export function TopStats({
  health,
  stale,
  connected,
  concStatus,
  flowStatus,
  avgOre,
  avgPoints,
  totalOre,
  shiftHours,
  flowNow,
  concNow,
}: {
  health: Health | null
  stale: boolean
  connected: boolean
  /** 浓度计最近一条读数的状态（正常/断线/超量程…），没有数据时 null */
  concStatus: string | null
  /** 流量计最近一条读数的状态，没有数据时 null */
  flowStatus: string | null
  /** 本区间平均干矿量（t/h）：逐点配对后取平均 */
  avgOre: number | null
  /** 参与平均的配对数（副标题里说明样本量） */
  avgPoints: number
  /** **本班**累计干矿量（t）= 本班平均干矿量 × 本班时长（先生 2026-11-05：按每个班累计） */
  totalOre: number | null
  /** 本班时长（小时）：已结束的班次 = 12；进行中的 = 开始→此刻 */
  shiftHours: number
  /** 当前流量（m³/h）与当前浓度（%）—— 算「瞬时干矿量」 */
  flowNow: number | null
  concNow: number | null
}) {
  // 「综合采集状态」是把两个传感器 + 链路健康状况收拢成一句话的地方
  // （先生 2026-11-04：各卡片里不要再各显各的状态与新鲜度）。
  const broken = [concStatus, flowStatus].filter((s) => s != null && s !== '正常')
  const tone = !health ? 'placeholder' : stale || broken.length ? 'warn' : 'ok'
  const value = !health ? '未取到' : broken.length ? '异常' : stale ? '无新数据' : '正常'

  const instantOre = dryOreMass(flowNow, concNow)
  const instantText = fitNum(instantOre, 2)
  const avgText = fitNum(avgOre, 2)
  // 累计按**每班**累计：本班平均干矿量 × 本班时长（先生 2026-11-05 定）。
  // 这样每个班一个数，班与班之间可直接比较；断流空洞不参与平均，也不需要"积分覆盖时长"。
  const shiftTotal = totalOre

  return (
    <div className="kpis top-stats">
      <div className="card kpi">
        <span className="label">综合干矿量</span>
        <span className={`value${avgOre == null ? ' placeholder' : ''}${avgText.compact ? ' compact' : ''}`}>
          {avgOre == null ? '—' : avgText.text}
          {avgOre != null && <span className="unit">t/h</span>}
        </span>
        <span className="sub">
          {avgOre == null
            ? '等待流量与浓度数据'
            : `本班累计 ≈ ${fmtNum(shiftTotal, 1)} t（${fmtNum(shiftHours, 1)} h × 均值）｜ ${avgPoints} 点配对`}
        </span>
      </div>
      <div className="card kpi">
        <span className="label">瞬时干矿量</span>
        <span className={`value${instantOre == null ? ' placeholder' : ''}${instantText.compact ? ' compact' : ''}`}>
          {instantOre == null ? '—' : instantText.text}
          {instantOre != null && <span className="unit">t/h</span>}
        </span>
        <span className="sub">
          {instantOre == null
            ? '等待流量与浓度数据'
            : `按 ${fmtNum(flowNow, 2)} m³/h ｜ 浓度 ${fmtNum(concNow, 2)}% 折算`}
        </span>
      </div>
      <div className="card kpi">
        <span className="label">综合采集状态</span>
        <span className={`value status-text ${tone}`}>{value}</span>
        <span className="sub">
          浓度 {concStatus ?? '—'} ｜ 流量 {flowStatus ?? '—'}
          {health ? ` ｜ ${stale ? '最后一条' : '滞后'} ${fmtAge(health.age_seconds)}` : ''}
          {health ? (connected ? ' ｜ 实时推送已连接' : ' ｜ 实时推送未连接') : ''}
        </span>
      </div>
    </div>
  )
}
