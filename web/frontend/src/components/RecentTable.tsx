import type { Reading } from '../api'
import { fmtDateTime, fmtNum } from '../api'
import { StatusPill } from './StatusPill'

/** 最近读数明细表（时间倒序，最新在最上）。 */
export function RecentTable({ rows }: { rows: Reading[] }) {
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
          {rows.map((r) => (
            <tr key={`${r.ts}-${r.raw}`}>
              <td>{fmtDateTime(r.ts)}</td>
              <td className="num">{r.raw}</td>
              <td className="num">{fmtNum(r.ma, 3)}</td>
              <td className="num">{fmtNum(r.conc_raw, 3)}</td>
              <td className="num">{fmtNum(r.conc, 3)}</td>
              <td>
                <StatusPill status={r.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
