import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发期：前端在 5173，浏览器只访问相对路径 /api/v1/...，由 Vite proxy 转发到本地
// FastAPI（127.0.0.1:8000）。生产期不经过这个代理——FastAPI 同源托管 dist。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})
