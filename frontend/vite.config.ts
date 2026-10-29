import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址（FastAPI）：uvicorn app.main:app 默认跑在 8000
const BACKEND = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    // 显式绑定 IPv4：默认的 localhost 在部分 Windows 上只监听 ::1，
    // 会导致 http://127.0.0.1:5173 打不开（浏览器用 localhost 一般没事）
    host: '127.0.0.1',
    port: 5173,
    // 开发时代理 /api 到后端，浏览器看到的是"同源请求"——
    // 这样既不用关心跨域，也不用把后端地址写进前端代码。
    // 注意：SSE（/api/tickets/{id}/stream）也会被正常代理（不缓冲）。
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
      },
    },
  },

  preview: {
    port: 4173,
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
      },
    },
  },
})
