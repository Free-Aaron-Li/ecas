import { useEffect, useRef, useState } from 'react'
import { fetchSession, kickDevice } from '../api'
import type { Session, SessionDevice } from '../api'

/**
 * 右上角的账户区（先生 2026-11-04）：
 *   · 显示「谁在登录」——中文姓名 + 用户名 + 角色；
 *   · 点开列出这个账户绑定的所有设备（哪台是当前这台），并允许把别的设备踢下线；
 *   · 退出登录。
 *
 * 只有站点挂在账户网关后面时才存在（`/api/session` 由 Worker 提供）；直连后端时
 * `session` 为 null，本组件返回 null —— 界面维持原来的令牌门，不会多出一个空壳按钮。
 */

function fmtMoment(value?: string): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function AccountMenu({ session }: { session: Session | null }) {
  const [open, setOpen] = useState(false)
  const [devices, setDevices] = useState<SessionDevice[]>(session?.devices ?? [])
  const [limit, setLimit] = useState<number>(session?.limit ?? 5)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const box = useRef<HTMLDivElement | null>(null)

  // 展开时拉一次最新的设备清单（别只看登录那一刻的）
  useEffect(() => {
    if (!open || !session) return
    let alive = true
    fetchSession()
      .then((s) => {
        if (!alive || !s) return
        setDevices(s.devices ?? [])
        setLimit(s.limit ?? 5)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [open, session])

  // 点外面 / Esc 关闭
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!session) return null

  const label = session.name || session.user
  const isAdmin = session.role === 'admin'

  const kick = async (id: string, name: string) => {
    if (!window.confirm(`把「${name}」这台设备从账户中移除？该设备会立刻退出登录。`)) return
    setBusy(id)
    setMsg(null)
    try {
      const next = await kickDevice(id)
      if (!next) {
        // 踢的是当前设备：网关已经把我们登出，直接回登录页
        window.location.href = '/login'
        return
      }
      setDevices(next.devices ?? [])
      setMsg('已移除该设备')
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="account" ref={box}>
      <button
        type="button"
        className={`account-btn${open ? ' open' : ''}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={`${label}（${session.user}）· 点击查看登录设备`}
      >
        <span className="account-dot" aria-hidden />
        <span className="account-name">{label}</span>
        {isAdmin ? <span className="account-role">管理员</span> : null}
        <span className="account-caret" aria-hidden>
          {open ? '▴' : '▾'}
        </span>
      </button>

      {open ? (
        <div className="account-panel" role="dialog" aria-label="账户与登录设备">
          <div className="account-head">
            <strong>{label}</strong>
            <span className="hint">
              用户名 {session.user} ｜ {isAdmin ? '管理员' : '普通账户'}
            </span>
          </div>

          <div className="account-section-title">
            登录设备 <span className="hint">{devices.length}/{limit} 台</span>
          </div>
          <ul className="account-devices">
            {devices.length === 0 ? <li className="hint">暂无记录</li> : null}
            {devices.map((d) => (
              <li key={d.id} className={d.current ? 'device current' : 'device'}>
                <div className="device-main">
                  <span className="device-name">
                    {d.name || '未知设备'}
                    {d.current ? <span className="device-tag">本机</span> : null}
                  </span>
                  <span className="device-meta">
                    {d.ip || '—'} ｜ 最近 {fmtMoment(d.last)}
                  </span>
                </div>
                {d.current ? (
                  <button
                    type="button"
                    className="link-btn"
                    disabled={busy !== ''}
                    onClick={() => void kick(d.id, d.name || '本机')}
                  >
                    移除本机
                  </button>
                ) : (
                  <button
                    type="button"
                    className="link-btn danger"
                    disabled={busy !== ''}
                    onClick={() => void kick(d.id, d.name || '该设备')}
                  >
                    {busy === d.id ? '移除中…' : '移除此设备'}
                  </button>
                )}
              </li>
            ))}
          </ul>
          {msg ? <p className="hint account-msg">{msg}</p> : null}
          <p className="hint account-note">
            同一账户同一时间只允许一台设备在线：在别的设备登录后，这台会自动退出。普通账户最多绑定 {limit} 台设备。
          </p>

          <div className="account-foot">
            {/* 先生 2026-11-04：所有账户都可自行管理密码（改密页要求先验原密码） */}
            <a className="header-btn link" href="/password" title="修改本账户密码">
              修改密码
            </a>
            {/* 用普通链接（GET）退出：表单 POST 后立刻 302，Safari 会把未消费的请求体
                当成连接复位，显示「无法打开网页」，要刷新才回到登录页（先生 2026-11-05）。
                服务端两种方法都支持，这里选最稳的那种。 */}
            <a className="header-btn" href="/logout" title="退出登录">
              退出登录
            </a>
          </div>
        </div>
      ) : null}
    </div>
  )
}
