import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    proxy: {
      '/api': {
        // 固定 IPv4，避免 Windows/Node 对 localhost 的双栈解析在后端启动阶段产生 AggregateError。
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      },
      '/business-api': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/business-api/, '/api')
      }
    }
  }
})
