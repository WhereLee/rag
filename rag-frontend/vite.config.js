import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api/auth': 'http://localhost:8082',
      '/api/chat': 'http://localhost:8082',
      '/api/ingest': 'http://localhost:8090',
      '/api/rag': 'http://localhost:8090',
      '/api/agent': 'http://localhost:8090',
      '/api/eval': 'http://localhost:8090',
      '/api/feedback': 'http://localhost:8090',
      '/api/admin': 'http://localhost:8090',
      '/api/diagnosis': 'http://localhost:8090',
    }
  }
})
