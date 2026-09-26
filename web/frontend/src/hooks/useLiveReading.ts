import { useEffect, useState } from 'react'
import type { Reading } from '../api'

/**
 * 订阅后端 `/ws/readings`：库里出现新的一行就推过来。
 * 断线自动重连（3 秒一次），组件卸载时关闭。
 */
export function useLiveReading(): { live: Reading | null; connected: boolean } {
  const [live, setLive] = useState<Reading | null>(null)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let ws: WebSocket | null = null
    let retry: number | undefined
    let disposed = false

    const connect = () => {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${window.location.host}/ws/readings`)

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
  }, [])

  return { live, connected }
}
