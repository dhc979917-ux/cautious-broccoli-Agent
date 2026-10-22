import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const proxy = {
  '/api/v1/ws': {
    target: 'ws://localhost:8001',
    ws: true,
    changeOrigin: true
  },
  '/api': {
    target: 'http://localhost:8001',
    changeOrigin: true
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'antd-vendor': ['antd', '@ant-design/icons'],
          'echarts-vendor': ['echarts'],
          'vis-vendor': ['vis-network'],
          'd3-vendor': ['d3-force'],
        },
      },
    },
  },
  server: {
    port: 5173,
    host: true,
    strictPort: true,
    proxy
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy
  },
  // Make sure anime.js is always pre-bundled for the dev server (avoids a
  // stale optimizeDeps cache silently breaking the motion module).
  optimizeDeps: {
    include: ['animejs']
  }
})
