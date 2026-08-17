import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import router from '../router'

// 创建两个 axios 实例：网关（8082）和 Python 直连（8090）
const gatewayClient = axios.create({
  baseURL: '',  // 通过 Vite proxy 转发
  timeout: 180000
})

const pythonClient = axios.create({
  baseURL: '',
  timeout: 180000
})

// 请求拦截器：注入 Authorization header
function authInterceptor(config) {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers['Authorization'] = `Bearer ${auth.token}`
  }
  return config
}

gatewayClient.interceptors.request.use(authInterceptor)
pythonClient.interceptors.request.use(authInterceptor)

// 响应拦截器：401 时自动跳转登录
function errorInterceptor(error) {
  if (error.response?.status === 401) {
    const auth = useAuthStore()
    auth.logout()
    router.push('/login')
  }
  return Promise.reject(error)
}

gatewayClient.interceptors.response.use(r => r, errorInterceptor)
pythonClient.interceptors.response.use(r => r, errorInterceptor)

// ===== Auth API（走网关 8082）=====
export const authApi = {
  register(username, password, role = 'user') {
    return gatewayClient.post('/api/auth/register', { username, password, role })
  },
  login(username, password) {
    return gatewayClient.post('/api/auth/login', { username, password })
  }
}

// ===== Chat API（走网关 8082）=====
export const chatApi = {
  ask(query, sessionId = '') {
    return gatewayClient.post('/api/chat/ask', { query, session_id: sessionId })
  },
  askStream(query, sessionId = '', onChunk) {
    // SSE 流式：使用 fetch 以便逐块读取
    const auth = useAuthStore()
    return fetch('/api/chat/ask-stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${auth.token}`
      },
      body: JSON.stringify({ query, session_id: sessionId, stream: true })
    }).then(response => {
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      function read() {
        return reader.read().then(({ done, value }) => {
          if (done) {
            onChunk({ type: 'done' })
            return
          }
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6).trim()
              if (data === '[DONE]') {
                onChunk({ type: 'done' })
                return
              }
              try {
                const parsed = JSON.parse(data)
                onChunk(parsed)
              } catch (e) {
                // skip malformed
              }
            }
          }
          return read()
        })
      }
      return read()
    })
  },
  history(sessionId) {
    return gatewayClient.get(`/api/chat/history/${sessionId}`)
  }
}

// ===== Ingest API（走 Python 8090）=====
export const ingestApi = {
  upload(file, replace = false) {
    const formData = new FormData()
    formData.append('file', file)
    return pythonClient.post(`/api/ingest/upload?replace=${replace}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  listDocuments() {
    return pythonClient.get('/api/ingest/documents')
  },
  documentStatus(docId) {
    return pythonClient.get(`/api/ingest/status/${docId}`)
  },
  documentChunks(docId, limit = 50) {
    return pythonClient.get(`/api/ingest/documents/${docId}/chunks`, { params: { limit } })
  },
  deleteDocument(docId) {
    return pythonClient.delete(`/api/ingest/documents/${docId}`)
  }
}

// ===== Feedback API =====
export const feedbackApi = {
  submit(qaLogId, rating, correction = '') {
    return pythonClient.post('/api/feedback', { qa_log_id: qaLogId, rating, correction })
  }
}

// ===== Admin API =====
export const adminApi = {
  listPrompts() {
    return pythonClient.get('/api/admin/prompts')
  },
  getPrompt(code) {
    return pythonClient.get(`/api/admin/prompts/${code}`)
  },
  submitChange(code, newContent) {
    return pythonClient.post(`/api/admin/prompts/${code}/change`, { new_content: newContent })
  },
  listApprovals() {
    return pythonClient.get('/api/admin/approvals')
  },
  resumeApproval(id, decision) {
    return pythonClient.post(`/api/admin/approvals/${id}/resume`, { decision })
  }
}

// ===== Eval API =====
export const evalApi = {
  seed() {
    return pythonClient.post('/api/eval/seed')
  },
  run(name = '', engine = 'baseline', withJudge = false) {
    return pythonClient.post('/api/eval/run', { name, engine, with_judge: withJudge })
  },
  listRuns() {
    return pythonClient.get('/api/eval/runs')
  },
  compare(runA, runB) {
    return pythonClient.get('/api/eval/compare', { params: { run_a: runA, run_b: runB } })
  }
}

// ===== Diagnosis API =====
export const diagnosisApi = {
  trigger() {
    return pythonClient.post('/api/diagnosis/trigger')
  },
  latest() {
    return pythonClient.get('/api/diagnosis/latest')
  },
  metrics() {
    return pythonClient.get('/api/diagnosis/metrics')
  }
}
