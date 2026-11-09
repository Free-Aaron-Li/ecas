import { useEffect, useMemo, useState } from 'react'
import {
  AuthError,
  exportShiftsUrl,
  exportWindowUrl,
  fetchShifts,
  fromLocalInput,
  toLocalInput,
} from '../api'
import type { ShiftsPage, ShiftWindow } from '../api'

/**
 * Excel 报表导出：
 *  - 「按班次」：白班 08:00–20:00 / 夜班 20:00–次日 08:00，整班切，可多班打包 zip；
 *  - 「自定义时段」：起止时刻精确到分钟，可跨班次、任意长短（≤30 天）。
 *
 * 两种模式都支持「完整报表 / 概述（汇报版）」：
 *  - 完整报表：区间统计、流量、综合干矿量、趋势图、报警事件 + 可选读数明细；
 *  - 概述：标题（不带型号）+ 时间/班次 + 四项核心指标 + 三张趋势图，给领导汇报用；
 *    同样可选「包含数据明细」，勾选时在概述页后附三张明细表。
 * 只是标题/文件名按「班次」或「时段」区分。
 *
 * 边界全部由后端按 `ECAS_TZ` 解释：这里只选日期/时刻，前端不自己算 12 小时窗口 ——
 * 否则浏览器时区一变，导出的班次就跟看板对不上。
 */

type Kind = 'day' | 'night'
type Mode = 'shift' | 'window'

const KINDS: { value: Kind; label: string }[] = [
  { value: 'day', label: '白班' },
  { value: 'night', label: '夜班' },
]

/** 与后端 `MAX_WINDOW_HOURS` 一致：自定义时段最长 30 天。 */
const MAX_WINDOW_HOURS = 720

/** 本地日期串 `YYYY-MM-DD`。班次按「天」选，具体几点由后端定。 */
function dayIso(d: Date): string {
  const pad = (n: number) => (n < 10 ? `0${n}` : String(n))
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 相对今天偏移若干天的日期串（负数=过去）。 */
function dayShift(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() + days)
  return dayIso(d)
}

/** `datetime-local` 输入值（精确到分钟，浏览器本地时区）。 */
function minuteInput(d: Date): string {
  return toLocalInput(d.toISOString())
}

function todayAt(hour: number, minute = 0): string {
  const d = new Date()
  d.setHours(hour, minute, 0, 0)
  return minuteInput(d)
}

function hoursAgo(hours: number): string {
  return minuteInput(new Date(Date.now() - hours * 3_600_000))
}

function toggleKind(prev: Kind[], value: Kind): Kind[] {
  return prev.includes(value) ? prev.filter((k) => k !== value) : [...prev, value]
}

export function ShiftExport() {
  const [mode, setMode] = useState<Mode>('shift')
  const [start, setStart] = useState(() => dayShift(0))
  const [end, setEnd] = useState(() => dayShift(0))
  const [kinds, setKinds] = useState<Kind[]>(['day', 'night'])
  // 先生 2026-11-04：报表要不要附「读数明细」表由导出人决定（不带时文件小很多）
  const [withDetail, setWithDetail] = useState(true)
  // 先生 2026-11-06：多一个「概述」选项，只出汇报用的四项指标 + 三张趋势图
  const [brief, setBrief] = useState(false)
  const [plan, setPlan] = useState<ShiftsPage | null>(null)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  // 自定义时段：默认今天 08:00 → 此刻（与白班起点对齐，先生 2026-11-06 要求精确到分钟）
  const [winStart, setWinStart] = useState(() => todayAt(8))
  const [winEnd, setWinEnd] = useState(() => minuteInput(new Date()))

  const kindsValue = useMemo(() => kinds.join(','), [kinds])

  // 参数不全就别发请求；错误在渲染期派生，省一次多余渲染（也能立刻显示出来）。
  const kindsEmpty = kinds.length === 0
  const blocked = !start || !end || kindsEmpty
  const error = fetchError ?? (kindsEmpty ? '至少要选白班或夜班中的一种' : null)

  // 预览：只算时间不查数据，所以随便改日期都能立刻回。
  useEffect(() => {
    if (mode !== 'shift' || blocked) return
    let alive = true
    setLoading(true)
    fetchShifts({ start, end, kinds: kindsValue })
      .then((page) => {
        if (!alive) return
        setPlan(page)
        setFetchError(null)
      })
      .catch((err: unknown) => {
        if (!alive) return
        setPlan(null)
        setFetchError(
          err instanceof AuthError
            ? '令牌无效或已过期，请重新输入'
            : err instanceof Error
              ? err.message
              : String(err),
        )
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [mode, start, end, kindsValue, blocked])

  const ready = !!plan && plan.count > 0
  const shiftWin: ShiftWindow = { start, end, kinds: kindsValue }
  const exportLabel = plan && plan.count > 1 ? `（${plan.count} 个班次 · zip）` : ''

  // —— 自定义时段：分钟精度、客户端先校验，后端还会再校验一遍 ——
  const winStartIso = fromLocalInput(winStart)
  const winEndIso = fromLocalInput(winEnd)
  const winStartMs = Date.parse(winStartIso)
  const winEndMs = Date.parse(winEndIso)
  const winHours = (winEndMs - winStartMs) / 3_600_000
  const windowError = !winStartIso || !winEndIso
    ? '请选择完整的起止时刻'
    : winEndMs <= winStartMs
      ? '结束时刻必须晚于开始时刻'
      : winHours > MAX_WINDOW_HOURS
        ? `自定义时段最长 ${MAX_WINDOW_HOURS} 小时（30 天），当前 ${winHours.toFixed(1)} 小时`
        : null
  const windowReady = !windowError
  const windowWin = { start: winStartIso, end: winEndIso }

  return (
    <section className="card">
      <div className="chart-head">
        <h2 className="card-title" style={{ margin: 0 }}>
          Excel 报表
        </h2>
        <span className="hint">
          {mode === 'shift'
            ? `白班 08:00–20:00 ｜ 夜班 20:00–次日 08:00（按 ${plan?.tz ?? 'Asia/Shanghai'} 切）`
            : `起止精确到分钟，可跨班次（按 ${plan?.tz ?? 'Asia/Shanghai'} 解释）`}
        </span>
      </div>

      <div className="range-panel">
        <div className="tabs" role="group" aria-label="导出方式">
          <button type="button" aria-pressed={mode === 'shift'} onClick={() => setMode('shift')}>
            按班次
          </button>
          <button type="button" aria-pressed={mode === 'window'} onClick={() => setMode('window')}>
            自定义时段
          </button>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }} role="group" aria-label="报表类型">
          <span className="hint">报表类型</span>
          <div className="tabs">
            <button type="button" aria-pressed={!brief} onClick={() => setBrief(false)}>
              完整报表
            </button>
            <button type="button" aria-pressed={brief} onClick={() => setBrief(true)}>
              概述（汇报版）
            </button>
          </div>
        </div>

        {mode === 'shift' ? (
          <>
            <label>
              起始日期
              <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label>
              结束日期
              <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
            <div className="tabs" role="group" aria-label="班次种类">
              {KINDS.map((k) => (
                <button
                  key={k.value}
                  type="button"
                  aria-pressed={kinds.includes(k.value)}
                  onClick={() => setKinds((prev) => toggleKind(prev, k.value))}
                >
                  {k.label}
                </button>
              ))}
            </div>
            <div className="range-quick">
              <button
                type="button"
                onClick={() => {
                  setStart(dayShift(0))
                  setEnd(dayShift(0))
                }}
              >
                今天
              </button>
              <button
                type="button"
                onClick={() => {
                  setStart(dayShift(-1))
                  setEnd(dayShift(-1))
                }}
              >
                昨天
              </button>
              <button
                type="button"
                onClick={() => {
                  setStart(dayShift(-6))
                  setEnd(dayShift(0))
                }}
              >
                近 7 天
              </button>
              <button
                type="button"
                onClick={() => {
                  setStart(dayShift(-29))
                  setEnd(dayShift(0))
                }}
              >
                近 30 天
              </button>
            </div>
          </>
        ) : (
          <>
            <label>
              起始时刻
              <input
                type="datetime-local"
                step={60}
                value={winStart}
                onChange={(e) => setWinStart(e.target.value)}
              />
            </label>
            <label>
              结束时刻
              <input
                type="datetime-local"
                step={60}
                value={winEnd}
                onChange={(e) => setWinEnd(e.target.value)}
              />
            </label>
            <div className="range-quick">
              <button
                type="button"
                onClick={() => {
                  setWinStart(hoursAgo(1))
                  setWinEnd(minuteInput(new Date()))
                }}
              >
                近 1 小时
              </button>
              <button
                type="button"
                onClick={() => {
                  setWinStart(hoursAgo(8))
                  setWinEnd(minuteInput(new Date()))
                }}
              >
                近 8 小时
              </button>
              <button
                type="button"
                onClick={() => {
                  setWinStart(hoursAgo(24))
                  setWinEnd(minuteInput(new Date()))
                }}
              >
                近 24 小时
              </button>
              <button
                type="button"
                onClick={() => {
                  setWinStart(todayAt(8))
                  setWinEnd(minuteInput(new Date()))
                }}
              >
                今天 08:00 起
              </button>
            </div>
          </>
        )}
      </div>

      <div className="range-bar">
        <label
          className="shift-detail"
          title={
            brief
              ? '不勾选时只出概述页（四项指标 + 三张趋势图）；勾选后另附读数 / 流量 / 干矿量三张明细表'
              : '不勾选时只出管理视图：综合汇总、三段具体数据与趋势、区间统计、报警；勾选后才附逐条读数/流量/干矿量明细表'
          }
        >
          <input type="checkbox" checked={withDetail} onChange={(e) => setWithDetail(e.target.checked)} />
          <span>包含数据明细</span>
        </label>
        <span className="range-now">
          {mode === 'shift' ? (
            error ? (
              ''
            ) : loading ? (
              '正在计算班次…'
            ) : plan ? (
              `共 ${plan.count} 个班次${plan.count > 1 ? '，导出为一个 zip' : '，导出为一份 xlsx'} ｜ 上限 ${plan.max_shifts} 班`
            ) : (
              ''
            )
          ) : windowError ? (
            ''
          ) : (
            `${winHours.toFixed(2)} 小时，导出为一份 xlsx`
          )}
        </span>
        {mode === 'shift' ? (
          ready ? (
            <a
              className="header-btn link"
              href={exportShiftsUrl(shiftWin, withDetail, brief)}
              download
              title={
                brief
                  ? `导出 ${plan?.count} 个班次的概述版 Excel（每班一份，含四项指标、三张趋势图${withDetail ? '与三张明细表' : ''}）`
                  : `导出 ${plan?.count} 个班次的 Excel（每班一份，含统计、趋势、流量与干矿量${withDetail ? '、逐条读数明细' : '，不含读数明细'}）`
              }
            >
              导出班次 Excel{exportLabel}
            </a>
          ) : (
            <button type="button" className="header-btn" disabled>
              导出班次 Excel
            </button>
          )
        ) : windowReady ? (
          <a
            className="header-btn link"
            href={exportWindowUrl(windowWin, withDetail, brief)}
            download
            title={
              brief
                ? `导出 ${winStart} ~ ${winEnd} 的概述版时段报表（含四项指标、三张趋势图${withDetail ? '与三张明细表' : ''}）`
                : `导出 ${winStart} ~ ${winEnd} 的时段报表（含统计、趋势、流量与干矿量${withDetail ? '、逐条读数明细' : '，不含读数明细'}）`
            }
          >
            导出自定义时段 Excel
          </a>
        ) : (
          <button type="button" className="header-btn" disabled>
            导出自定义时段 Excel
          </button>
        )}
      </div>

      {mode === 'shift' && plan && plan.count > 0 && (
        <div className="shift-list">
          {plan.shifts.map((s) => (
            <span key={s.slug} className="shift-chip" title={`导出文件名：${s.filename}`}>
              {s.label_long}
              {!s.complete && <em>进行中</em>}
            </span>
          ))}
        </div>
      )}

      {mode === 'shift' && error && <span className="range-err">{error}</span>}
      {mode === 'window' && windowError && <span className="range-err">{windowError}</span>}

      {brief ? (
        <span className="hint">
          概述版（汇报用）含：报表标题（不带型号）｜ 时间 ｜ {mode === 'shift' ? '班次' : '时段'} ｜ 综合干矿量 ｜
          本{mode === 'shift' ? '班次' : '时段'}累计 ｜ 平均浓度 ｜ 平均流量 ｜ 干矿量 / 浓度 / 流量三张趋势图
          {withDetail ? '，另附读数 / 流量 / 干矿量三张明细表' : '（未勾选数据明细）'}。
          {mode === 'window' ? ' 自定义时段可跨班次、精确到分钟。' : ''}
        </span>
      ) : (
        <span className="hint">
          每份报表含：口径与阈值 ｜ 综合汇总（干矿量·浓度·流量）｜ 具体干矿量 + 趋势 ｜ 具体浓度 + 趋势
          ｜ 具体流量 + 趋势 ｜ 区间统计 ｜ 报警事件
          {withDetail
            ? ' ｜ 逐条读数明细（有数据时另附「流量明细」「干矿量明细」表）'
            : '（未勾选数据明细：读数/流量/干矿量三张明细表都不生成）'}。
          {mode === 'window' ? ' 自定义时段可跨班次、精确到分钟。' : ''} 多班次打包需要几秒到几十秒，点了之后等浏览器出下载。
        </span>
      )}
    </section>
  )
}
