import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * Vite dev proxy。
 *
 * 第一轮修复（安全收口）：前端所有业务 API 统一走 Java 网关（user_id 由网关从 JWT
 * 提取并签名注入，直连 8090 伪造身份已被 X-Gateway-Sign 校验拒绝）。
 * 因此删除原 8090 直连规则（/api/ingest /api/rag /api/agent /api/eval
 * /api/feedback /api/diagnosis），只保留网关与认证两条通道：
 *   /api/auth, /api/chat   —— 网关直通端点
 *   /api/admin/proxy/**    —— 网关白名单代理（文档/上传/任务/反馈/评估/诊断/admin）
 * 后端 Python 侧仍保留 8090 直连能力仅用于内部脚本/运维，浏览器一律经网关。
 */
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    host: '127.0.0.1',   // 只监听 IPv4，避免 localhost 解析到 ::1 导致 127.0.0.1 连不上
    proxy: {
      '/api/auth': {
        target: 'http://localhost:8082',
        changeOrigin: true
      },
      '/api/files': {
        target: 'http://localhost:8082',
        changeOrigin: true
      },
      '/api/dirs': {
        target: 'http://localhost:8082',
        changeOrigin: true
      },
      '/api/chat': {
        target: 'http://localhost:8082',
        changeOrigin: true
      },
      '/api/qa': {
        target: 'http://localhost:8082',
        changeOrigin: true
      },
      '/api/admin': {
        target: 'http://localhost:8082',
        changeOrigin: true
      }
    }
  }
})