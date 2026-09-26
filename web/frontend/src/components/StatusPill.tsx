import type { Status } from '../api'
import { pillClass } from '../api'

/** 状态徽标：带状态点，颜色由后端给的四档状态决定。 */
export function StatusPill({ status, label }: { status: Status | string; label?: string }) {
  return (
    <span className={`pill ${pillClass(status)}`}>
      <span className="dot" aria-hidden="true" />
      {label ?? status}
    </span>
  )
}

/** 连接/新鲜度类徽标（不用状态色语义，但复用样式）。 */
export function InfoPill({ tone, text }: { tone: 'ok' | 'warn' | 'bad' | 'dim'; text: string }) {
  return (
    <span className={`pill ${tone}`}>
      <span className="dot" aria-hidden="true" />
      {text}
    </span>
  )
}
