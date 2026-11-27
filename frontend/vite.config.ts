import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 双目标：一份源码
//  desktop/mobile build → 静态资源由 FastAPI /static 托管（相对路径）
//  dev                  → proxy 到 127.0.0.1:8765
export default defineConfig({
  base: './',
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true }
    }
  },
  build: {
    outDir: 'dist',
    target: 'es2020',
    chunkSizeWarningLimit: 500,
    // ECharts/lightweight-charts 均通过动态 import() 引入，
    // Rollup 自动按需切 chunk（echarts/core+4 charts ≈ 按需注册），首屏不取。
  }
})
