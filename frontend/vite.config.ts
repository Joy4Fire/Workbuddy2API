import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

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
      // 开发时把 /admin 和 /v1 代理到后端
      '/admin': { target: 'http://127.0.0.1:8787', changeOrigin: true },
      '/v1': { target: 'http://127.0.0.1:8787', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('ant-design-vue')) return 'vendor-antd'
            if (id.includes('@ant-design/icons-vue')) return 'vendor-icons'
            if (id.includes('vue-router') || id.includes('node_modules/vue')) return 'vendor-vue'
            if (id.includes('axios') || id.includes('dayjs')) return 'vendor-util'
            return 'vendor'
          }
        },
      },
    },
  },
})
