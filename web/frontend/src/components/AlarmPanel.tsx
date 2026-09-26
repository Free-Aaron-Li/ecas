import { useEffect, useState } from 'react'
import { fmtDateTime, fmtNum, resetAlarms, saveAlarms, scanAlarms } from '../api'
import type { AlarmEvent, Alarms, AlarmsPage } from '../api'

/**
 * 报警面板（第 3 步）：自定义上下限 + 进行中报警 + 事件表。
 *
 * 阈值**存后端**（`alarm_config` 表），不是浏览器本地：这样看板的线、统计的越限数、
 * 引擎记的事件、导出的注释头用的是同一个数。保存/恢复成功后由 `onChanged()` 让上层
 * 重拉 params/summary/readings，图表与统计条立刻跟着变。
 */
export function AlarmPanel({
  alarms,
  page,
  hours,
  onChanged,
}: {
  alarms?: Alarms | null
  page: AlarmsPage | null
  hours: number
  onChanged: () => void
}) {
  const [low, setLow] = useState('')
  const [high, setHigh] = useState('')
  const [busy, setBusy] = useState<'' | 'save' | 'reset' | 'scan'>('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // 后端阈值变了（首次加载、别人改了、刚恢复默认）就把输入框对齐过去
  useEffect(() => {
    if (!alarms) return
    setLow(String(alarms.low))
    setHigh(String(alarms.high))
  }, [alarms?.low, alarms?.high, alarms])

  const active = (page?.items ?? []).filter((e) => e.active)
  const dirty = alarms ? Number(low) !== alarms.low || Number(high) !== alarms.high : false

  const run = async (kind: 'save' | 'reset' | 'scan') => {
    setBusy(kind)
    setMsg(null)
    setErr(null)
    try {
      if (kind === 'save') {
        const a = await saveAlarms(Number(low), Number(high))
        setMsg(`已保存：${fmtNum(a.low, 1)} ~ ${fmtNum(a.high, 1)} 浓度（raw ${a.low_raw} ~ ${a.high_raw}）`)
      } else if (kind === 'reset') {
        const a = await resetAlarms()
        setMsg(`已恢复出厂默认：${fmtNum(a.low, 1)} ~ ${fmtNum(a.high, 1)} 浓度`)
      } else {
        const r = await scanAlarms()
        setMsg(`已扫描：新开 ${(r.opened as string[] | undefined)?.length ?? 0} 条、关闭 ${(r.closed as string[] | undefined)?.length ?? 0} 条`)
      }
      onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  return (
    <section className="card">
      <div className="chart-head">
        <h2 className="card-title" style={{ margin: 0 }}>
          报警（规则跑在后端）
        </h2>
        <span className="hint">
          当前用{alarms?.custom ? '自定义' : '出厂默认'}值：{fmtNum(alarms?.low, 1)} ~ {fmtNum(alarms?.high, 1)} 浓度
          {alarms?.custom && alarms.default_low != null
            ? `（默认 ${fmtNum(alarms.default_low, 1)} ~ ${fmtNum(alarms.default_high, 1)}）`
            : ''}
          ｜ raw 判据 {alarms?.low_raw ?? '—'} ~ {alarms?.high_raw ?? '—'}
        </span>
      </div>

      <div className="alarm-form">
        <label>
          <span>下限（低于它报警 · 红）</span>
          <input
            type="number"
            step="0.1"
            min="0"
            max="100"
            className="alarm-input"
            value={low}
            onChange={(e) => setLow(e.target.value)}
          />
        </label>
        <label>
          <span>上限（高于它报警 · 黄）</span>
          <input
            type="number"
            step="0.1"
            min="0"
            max="100"
            className="alarm-input"
            value={high}
            onChange={(e) => setHigh(e.target.value)}
          />
        </label>
        <div className="alarm-actions">
          <button
            type="button"
            className="header-btn"
            disabled={busy !== '' || !alarms || !dirty}
            onClick={() => void run('save')}
          >
            {busy === 'save' ? '保存中…' : '保存'}
          </button>
          <button
            type="button"
            className="header-btn"
            disabled={busy !== '' || !alarms?.custom}
            onClick={() => void run('reset')}
          >
            恢复默认
          </button>
          <button type="button" className="header-btn" disabled={busy !== ''} onClick={() => void run('scan')}>
            {busy === 'scan' ? '扫描中…' : '立即扫描'}
          </button>
        </div>
      </div>

      {msg && <div className="alarm-note ok">{msg}</div>}
      {err && <div className="alarm-note bad">操作失败：{err}</div>}

      <div className="alarm-active">
        <span className="label">进行中 {active.length ? `（${active.length}）` : ''}</span>
        {active.length ? (
          active.map((e) => <ActiveChip key={e.id} event={e} />)
        ) : (
          <span className="hint">当前没有进行中的报警</span>
        )}
        <span className="hint">
          {page?.scan ? `最近一次扫描：${describeScan(page.scan)}` : '后端尚未扫描过'}
        </span>
      </div>

      <table>
        <thead>
          <tr>
            <th>开始时间</th>
            <th>种类</th>
            <th>级别</th>
            <th>持续</th>
            <th className="num">峰值 raw</th>
            <th className="num">峰值浓度</th>
            <th className="num">行数</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {(page?.items ?? []).map((e) => (
            <tr key={e.id} className={e.active ? 'row-alarm' : undefined}>
              <td>{fmtDateTime(e.started_ts)}</td>
              <td>
                {e.kind_label}
                {e.note ? <span className="hint"> {e.note}</span> : null}
              </td>
              <td className={e.level === '严重' ? 'alarm-low' : 'alarm-high'}>{e.level}</td>
              <td>{fmtDuration(e.duration_seconds)}</td>
              <td className="num">{e.peak_raw ?? '—'}</td>
              <td className="num">{fmtNum(e.peak_conc, 2)}</td>
              <td className="num">{e.n_rows}</td>
              <td>{e.active ? '进行中' : `已结束 ${e.ended_ts ? fmtDateTime(e.ended_ts) : ''}`}</td>
            </tr>
          ))}
          {!(page?.items ?? []).length && (
            <tr>
              <td colSpan={8} className="hint">
                近 {hours} 小时没有报警记录
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  )
}

function ActiveChip({ event }: { event: AlarmEvent }) {
  const cls = event.kind === 'conc_low' || event.kind === 'break' || event.kind === 'no_data' ? 'low' : 'high'
  return (
    <span className={`alarm-tag ${cls}`}>
      {event.kind_label} ｜ {fmtDuration(event.duration_seconds)}
      {event.peak_conc != null ? ` ｜ 峰值 ${fmtNum(event.peak_conc, 2)}` : ''}
    </span>
  )
}

function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s} 秒`
  if (s < 5400) return `${(s / 60).toFixed(1)} 分钟`
  if (s < 172800) return `${(s / 3600).toFixed(1)} 小时`
  return `${(s / 86400).toFixed(1)} 天`
}

/** 后端 `POST /api/alarms/scan` / 后台循环的返回摘要（字段是动态的，展示上做兜底）。 */
function describeScan(scan: Record<string, unknown>): string {
  if (typeof scan.error === 'string') return `失败（${scan.error}）`
  const opened = (scan.opened as string[] | undefined)?.length ?? 0
  const closed = (scan.closed as string[] | undefined)?.length ?? 0
  const rows = typeof scan.n_rows === 'number' ? scan.n_rows : '—'
  return `新开 ${opened} ｜ 关闭 ${closed} ｜ 本次看 ${rows} 行`
}
