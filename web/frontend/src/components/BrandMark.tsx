/**
 * 页头品牌标记：仪表盘（半圆刻度 + 指针）+ 深色圆角底牌。
 *
 * 2026-11-04 先生要求「logo 统一」：这里的画法与 `public/favicon.svg`
 * **逐元素一一对应**（圆角底牌、边框、灰弧、绿弧、白指针、绿圆心），
 * 改一处必须同步改另一处，否则页头和标签页会长得不一样。
 *
 * 内联 SVG 而不是 `<img src="/favicon.svg">`：页头不额外发请求、颜色跟着主题变量走，
 * 缩放也永远清晰。
 */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      {/* 底牌：与 favicon 的 <rect rx="14"> 一致 */}
      <rect width="64" height="64" rx="14" fill="var(--bg)" />
      <rect
        x="0.75"
        y="0.75"
        width="62.5"
        height="62.5"
        rx="13.25"
        fill="none"
        stroke="var(--border-strong)"
        strokeWidth="1.5"
      />
      <path
        d="M11 46a21 21 0 0 1 42 0"
        stroke="var(--border-strong)"
        strokeWidth="6"
        strokeLinecap="round"
      />
      <path
        d="M11 46a21 21 0 0 1 9.4-17.5"
        stroke="var(--accent)"
        strokeWidth="6"
        strokeLinecap="round"
      />
      <path d="M32 46 44 30" stroke="var(--fg)" strokeWidth="4" strokeLinecap="round" />
      <circle cx="32" cy="46" r="5.5" fill="var(--accent)" />
      <circle cx="32" cy="46" r="2.2" fill="var(--bg)" />
    </svg>
  )
}
