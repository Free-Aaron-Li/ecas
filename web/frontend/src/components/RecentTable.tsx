import type { Alarms, Reading } from '../api'
import { alarmSide, fmtDateTime, fmtNum } from '../api'
import { StatusPill } from './StatusPill'

/** 最近读数明细表（时间倒序，最新在最上）。越限行在浓度列给出标记。 */
export function RecentTable({ rows, alarms }: { rows: Reading[]; alarms?: Alarms | null }) {
  if (!rows.length) return <div className="skeleton">暂无读数。</div>

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th className="num">原始值</th>
            <th className="num">电流 mA</th>
            <th className="num">未补偿</th>
            <th className="num">浓度</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const side = r.status === '正常' ? alarmSide(r.conc, alarms) : null
            return (
              <tr key={`${r.ts}-${r.raw}`} className={side ? 'row-alarm' : undefined}>
                <td>{fmtDateTime(r.ts)}</td>
                <td className="num">{r.raw}</td>
                <td className="num">{fmtNum(r.ma, 3)}</td>
                <td className="num">{fmtNum(r.conc_raw, 3)}</td>
                <td className="num">
                  {fmtNum(r.conc, 3)}
                  {side && (
                    <span className={side === 'high' ? 'alarm-high' : 'alarm-low'}>
                      {side === 'high' ? ' ↑越限' : ' ↓越限'}
                    </span>
                  )}
                </td>
                <td>
                  <StatusPill status={r.status} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
