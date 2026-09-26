import type { Alarms, Params, Reading } from '../api'
import { alarmSide, fmtDateTime, fmtNum, trimNum } from '../api'
import { StatusPill } from './StatusPill'

/** 顶部四张 KPI：浓度（主）/ 电流 / 原始值 / 状态与时刻。 */
export function KpiCards({
  last,
  params,
  alarms,
}: {
  last: Reading | null
  params: Params | null
  alarms?: Alarms | null
}) {
  if (!last) {
    return (
      <div className="kpis">
        <div className="card kpi">
          <span className="label">当前浓度</span>
          <span className="skeleton">正在读取数据…</span>
        </div>
      </div>
    )
  }

  const offsetText =
    params?.offset_mode === 'mul'
      ? `×${(1 + (params?.offset_pct ?? 0) / 100).toFixed(4)}`
      : `+${trimNum(params?.offset_conc ?? 0)}`

  // 越限只在「正常」档才有意义（断线/超量程是设备级异常，那条浓度是外推值）
  const side = last.status === '正常' ? alarmSide(last.conc, alarms) : null

  return (
    <div className="kpis">
      <div className="card kpi primary">
        <span className="label">当前浓度</span>
        <span className="value">
          {fmtNum(last.conc, 3)}
          <span className="unit">浓度</span>
        </span>
        <span className="sub">
          未补偿 {fmtNum(last.conc_raw, 3)} ｜ 补偿 {offsetText}
        </span>
        {side && (
          <span className={side === 'high' ? 'alarm-tag high' : 'alarm-tag low'}>
            {side === 'high'
              ? `高于报警上限 ${fmtNum(alarms?.high ?? 0, 1)}`
              : `低于报警下限 ${fmtNum(alarms?.low ?? 0, 1)}`}
          </span>
        )}
      </div>

      <div className="card kpi">
        <span className="label">电流</span>
        <span className="value">
          {fmtNum(last.ma, 3)}
          <span className="unit">mA</span>
        </span>
        <span className="sub">原始值 ÷ 1000</span>
      </div>

      <div className="card kpi">
        <span className="label">原始值（uint16）</span>
        <span className="value">{last.raw}</span>
        <span className="sub">
          {params
            ? `${params.range.i_lo}~${params.range.i_hi} mA ↔ ${params.range.c_lo}~${params.range.c_hi}`
            : '4~20 mA ↔ 0~100'}
        </span>
      </div>

      <div className="card kpi">
        <span className="label">状态</span>
        <span className="value">
          <StatusPill status={last.status} />
        </span>
        <span className="sub">数据时刻 {fmtDateTime(last.ts)}</span>
      </div>
    </div>
  )
}
