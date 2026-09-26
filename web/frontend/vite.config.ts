import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发时把 /api 与 /ws 代理到本地 FastAPI 后端（127.0.0.1:8080）；
// 前端不硬编码后端地址，生产部署时同一份代码由后端托管 dist/ 即可。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
