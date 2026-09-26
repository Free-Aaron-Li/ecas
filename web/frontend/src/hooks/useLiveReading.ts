import { useEffect, useState } from 'react'
import { wsUrl } from '../api'
import type { Reading } from '../api'

/**
 * 订阅后端 `/ws/readings`：库里出现新的一行就推过来。
 * 断线自动重连（3 秒一次），组件卸载时关闭。
 * `token` 变化（首次输入 / 清除令牌）会重建连接 —— 令牌走查询串，浏览器不能给 WS 带请求头。
 */
export function useLiveReading(token = ''): { live: Reading | null; connected: boolean } {
  const [live, setLive] = useState<Reading | null>(null)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let ws: WebSocket | null = null
    let retry: number | undefined
    let disposed = false

    const connect = () => {
      ws = new WebSocket(wsUrl('/ws/readings', token))

      ws.onopen = () => setConnected(true)
      ws.onmessage = (event) => {
        try {
          setLive(JSON.parse(event.data as string) as Reading)
        } catch {
          /* 坏帧忽略即可，下一帧会覆盖 */
        }
      }
      ws.onclose = () => {
        setConnected(false)
        if (!disposed) retry = window.setTimeout(connect, 3000)
      }
      ws.onerror = () => ws?.close()
    }

    connect()
    return () => {
      disposed = true
      window.clearTimeout(retry)
      ws?.close()
    }
  }, [token])

  return { live, connected }
}
