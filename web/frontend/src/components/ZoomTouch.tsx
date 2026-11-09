import { useRef } from 'react'
import type { PointerEvent as ReactPointerEvent, RefObject } from 'react'

/** 框选后允许的最小时间跨度：再窄就没意义了（还会因为点数太少画不出来）。 */
const MIN_SPAN_MS = 5 * 60_000

/**
 * 趋势图下方的「放大缩小」触摸层（先生 2026-11-04 反馈：手机上浓度计的滑块拖不动，流量计却可以）。
 *
 * 原因：Recharts 自带的 Brush 只用 **mouse 事件**（mousedown/mousemove）驱动。手机上一个横向拖动
 * 会先被浏览器判成「滚动页面」，等 touchend 才补发鼠标事件 —— 拖动过程中的 mousemove 全丢了，
 * 于是滑块看起来"按不动"。桌面（鼠标）没有这个问题。
 *
 * 做法：在 Brush 那条 24px 高的带子上盖一层透明 div，用 **Pointer Events**（鼠标/触摸/手写笔同一套）
 * 自己做拖拽映射：拖两端改边界、拖中间平移，并 `touch-action: none` 把触摸从页面滚动手里抢过来。
 * Brush 本身仍照常画（视觉不变），只是交互交给这一层。
 */
export function ZoomTouch({
  boxRef,
  base,
  domainStart,
  domainEnd,
  onEdges,
}: {
  boxRef: RefObject<HTMLDivElement | null>
  /** 整个班次的可选范围（Brush 的两端） */
  base: [number, number] | null
  /** 当前显示范围（框选后的结果；未框选时等于 base） */
  domainStart: number
  domainEnd: number
  onEdges: (start: number, end: number) => void
}) {
  const drag = useRef<{
    mode: 'start' | 'end' | 'pan'
    x0: number
    s0: number
    e0: number
    left: number
    width: number
    moved: boolean
  } | null>(null)

  /** Brush 中间那条「可拖动带」的实际屏幕坐标 —— 用它把像素折算成时间，和 Recharts 的画法一致。 */
  const slideRect = (): DOMRect | null => {
    const box = boxRef.current
    if (!box) return null
    const slide = box.querySelector('.recharts-brush-slide')
    const r = slide?.getBoundingClientRect()
    return r && r.width > 0 ? r : null
  }

  /**
   * 拖动必须是**相对位移**（先生 2026-11-04：「一碰滑块就跳」）。
   * 之前用「把边界设成手指所在时刻」，手指落点离端点几像素、甚至只是轻触，边界就"跳"到手指下面；
   * 手指再抖一下又是一跳。现在登记按下时的位置，之后只按位移量平移，且要超过 6px 才算拖动。
   */
  const onPointerDown = (ev: ReactPointerEvent<HTMLDivElement>) => {
    const rect = slideRect()
    if (!rect || !base) return
    ev.stopPropagation()
    const span = base[1] - base[0]
    const xStart = rect.left + ((domainStart - base[0]) / span) * rect.width
    const xEnd = rect.left + ((domainEnd - base[0]) / span) * rect.width
    const grab = 30 // 手指比鼠标粗：离端点 30px 内都算抓端点
    const mode: 'start' | 'end' | 'pan' =
      Math.abs(ev.clientX - xStart) <= grab
        ? 'start'
        : Math.abs(ev.clientX - xEnd) <= grab
          ? 'end'
          : 'pan'
    drag.current = {
      mode,
      x0: ev.clientX,
      s0: domainStart,
      e0: domainEnd,
      left: rect.left,
      width: rect.width,
      moved: false,
    }
    ev.currentTarget.setPointerCapture(ev.pointerId)
  }

  const onPointerMove = (ev: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current
    if (!d || !base) return
    const dx = ev.clientX - d.x0
    if (!d.moved) {
      if (Math.abs(dx) < 6) return // 轻触/手抖：不动
      d.moved = true
    }
    ev.preventDefault()
    ev.stopPropagation()
    const span = base[1] - base[0]
    const dt = (dx / d.width) * span
    if (d.mode === 'pan') {
      // 平移要**保住跨度**（只挪窗口，不拉伸）：否则拖到边界时会变成"越拖越宽"
      const win = d.e0 - d.s0
      const s2 = Math.min(Math.max(d.s0 + dt, base[0]), Math.max(base[0], base[1] - win))
      onEdges(s2, s2 + win)
      return
    }
    if (d.mode === 'start') {
      const s2 = Math.min(Math.max(d.s0 + dt, base[0]), d.e0 - MIN_SPAN_MS)
      onEdges(s2, d.e0)
    } else {
      const e2 = Math.max(Math.min(d.e0 + dt, base[1]), d.s0 + MIN_SPAN_MS)
      onEdges(d.s0, e2)
    }
  }

  const onPointerUp = (ev: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current) {
      drag.current = null
      if (ev.currentTarget.hasPointerCapture(ev.pointerId)) {
        ev.currentTarget.releasePointerCapture(ev.pointerId)
      }
    }
  }

  return (
    <div
      className="brush-touch"
      role="presentation"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    />
  )
}
