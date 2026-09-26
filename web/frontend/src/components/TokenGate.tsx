import { useState } from 'react'

/**
 * 令牌门：后端设了 `ECAS_AUTH_TOKEN` 而浏览器没有（或不对）时显示。
 * 令牌只存在 localStorage，提交后由 `App` 重新拉数并重建 WebSocket。
 */
export function TokenGate({
  onSubmit,
  onSkip,
  error,
}: {
  onSubmit: (token: string) => void
  onSkip?: () => void
  error?: string | null
}) {
  const [value, setValue] = useState('')

  return (
    <div className="gate">
      <form
        className="card gate-card"
        onSubmit={(e) => {
          e.preventDefault()
          if (value.trim()) onSubmit(value.trim())
        }}
      >
        <h2 className="card-title" style={{ margin: 0 }}>
          需要访问令牌
        </h2>
        <p className="gate-hint">
          后端启用了令牌校验（<code>ECAS_AUTH_TOKEN</code>）。令牌由部署者提供，
          只保存在本机浏览器，不会发往其它地方。
        </p>
        <input
          className="gate-input"
          type="password"
          autoFocus
          autoComplete="current-password"
          placeholder="粘贴访问令牌"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        {error && <div className="gate-error">{error}</div>}
        <div className="gate-actions">
          <button type="submit" className="gate-btn" disabled={!value.trim()}>
            进入看板
          </button>
          {onSkip && (
            <button type="button" className="gate-btn ghost" onClick={onSkip}>
              只用公开探针
            </button>
          )}
        </div>
      </form>
    </div>
  )
}
