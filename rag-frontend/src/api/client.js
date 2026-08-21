import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import router from '../router'

// 统一走 Java 网关（8082，经 Vite proxy 转发）。
// 第一轮修复（安全收口）：不再直连 Python 8090 —— user_id 由网关从 JWT 提取并签名注入，
// 前端伪造 X-User-Id 不再可信。对应 Python 侧 X-Gateway-Sign 签名校验。
const http = axios.create({
  baseURL: '',  // 通过 Vite proxy 转发
  timeout: 180000
})

// 请求拦截器：注入 Authorization
function authInterceptor(config) {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers['Authorization'] = `Bearer ${auth.token}`
  }
  return config
}
http.interceptors.request.use(authInterceptor)

// 响应拦截器：401 时自动跳转登录
http.interceptors.response.use(r => r, error => {
  if (error.response?.status === 401) {
    const auth = useAuthStore()
    auth.logout()
    router.push('/login')
  }
  return Promise.reject(error)
})

// ===== Auth API（网关直通端点）=====
export const authApi = {
  register(username, password, role = 'user') {
    return http.post('/api/auth/register', { username, password, role })
  },
  login(username, password) {
    return http.post('/api/auth/login', { username, password })
  }
}

// ===== Chat API =====
export const chatApi = {
  ask(query, sessionId = '') {
    return http.post('/api/chat/ask', { query, session_id: sessionId })
  },
  askStream(query, sessionId = '', onChunk) {
    // SSE 流式：使用 fetch 以便逐块读取（经 Vite proxy 到网关 8082）
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
            // Spring SseEmitter 输出 data:{...}（无空格），Python 直出 data: {...}（有空格），统一兼容
            const m = line.match(/^data:\s*(.*)$/)
            if (m) {
              const data = m[1].trim()
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
    return http.get(`/api/chat/history/${sessionId}`)
  }
}

// ===== Qa API（新链路：网关 /api/qa/ask → Python 8091 SSE 透传）=====
export const qaApi = {
  askStream(query, sessionId = '', onChunk, thinking = true, signal = null) {
    // SSE 流式：fetch 逐块读取（经 Vite proxy 到网关 8082）
    // signal：停止生成（AbortController.abort() 中断连接，后端断开并落库部分回答）
    const auth = useAuthStore()
    return fetch('/api/qa/ask', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${auth.token}`
      },
      body: JSON.stringify({ query, session_id: sessionId, thinking }),
      signal
    }).then(async response => {
      if (!response.ok) {
        const text = await response.text()
        throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`)
      }
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
            // Spring SseEmitter 输出 data:{...}（无空格），Python 直出 data: {...}（有空格），统一兼容
            const m = line.match(/^data:\s*(.*)$/)
            if (m) {
              try {
                onChunk(JSON.parse(m[1].trim()))
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
  }
}

// ===== Dir API（单层目录：建/列/重命名/删除，user_id 由服务端从 JWT 注入）=====
export const dirApi = {
  list() {
    return http.get('/api/dirs')
  },
  create(name) {
    return http.post('/api/dirs', { name })
  },
  rename(id, name) {
    return http.patch(`/api/dirs/${id}`, { name })
  },
  remove(id) {
    return http.delete(`/api/dirs/${id}`)
  }
}

// ===== Session API（会话：目录下多个对话，对话内多轮记忆）=====
export const sessionApi = {
  create(dirId = null, summary = '') {
    return http.post('/api/qa/sessions', { dir_id: dirId, summary })
  },
  list(dirId = null) {
    const params = dirId != null ? { dir_id: dirId } : {}
    return http.get('/api/qa/sessions', { params })
  },
  history(sessionId) {
    return http.get(`/api/qa/sessions/${sessionId}/history`)
  }
}

// ===== File API（我的文件：上传/列表/下载/删除，user_id 由服务端从 JWT 注入）=====
export const fileApi = {
  upload(file, dirId = null) {
    const formData = new FormData()
    formData.append('file', file)
    if (dirId != null) {
      formData.append('dir_id', dirId)
    }
    return http.post('/api/files/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  list(page = 1, pageSize = 20, dirId = null) {
    const params = { page, pageSize }
    if (dirId != null) {
      params.dir_id = dirId
    }
    return http.get('/api/files', { params })
  },
  move(id, dirId) {
    return http.patch(`/api/files/${id}/move`, { dir_id: dirId })
  },
  rename(id, filename) {
    return http.put(`/api/files/${id}/rename`, { filename })
  },
  checkHash(hash, size, filename) {
    return http.post('/api/files/check-hash', { hash, size, filename })
  },
  uploadInit(hash, size, filename, chunkCount, chunkSize) {
    return http.post('/api/files/upload/init', { hash, size, filename, chunk_count: chunkCount, chunk_size: chunkSize })
  },
  uploadChunk(uploadId, index, blob) {
    const fd = new FormData()
    fd.append('file', blob)
    return http.post(`/api/files/upload/${uploadId}/chunk?index=${index}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  uploadStatus(uploadId) {
    return http.get(`/api/files/upload/${uploadId}/status`)
  },
  uploadComplete(uploadId) {
    return http.post(`/api/files/upload/${uploadId}/complete`)
  },
  preview(id) {
    return http.get(`/api/files/${id}/preview`)
  },
  reparse(id) {
    return http.post(`/api/files/${id}/reparse`)
  },
  trash() {
    return http.get('/api/files/trash')
  },
  restore(id) {
    return http.post(`/api/files/${id}/restore`)
  },
  remove(id) {
    return http.delete(`/api/files/${id}`)
  },
  async download(id, filename) {
    const resp = await http.get(`/api/files/${id}/download`, { responseType: 'blob' })
    const url = URL.createObjectURL(resp.data)
    const a = document.createElement('a')
    a.href = url
    a.download = filename || 'file'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }
}

// ===== Ingest API（走网关代理，user_id 服务端注入；上传异步任务化）=====
export const ingestApi = {
  upload(file, replace = false) {
    const formData = new FormData()
    formData.append('file', file)
    if (replace) {
      formData.append('replace', 'true')
    }
    // 202 语义：立即返回 { job_id, status:"queued" }；或去重快路径 200
    return http.post('/api/admin/proxy/api/ingest/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  job(jobId) {
    return http.get(`/api/admin/proxy/api/ingest/jobs/${jobId}`)
  },
  listJobs(limit = 10) {
    return http.get('/api/admin/proxy/api/ingest/jobs', { params: { limit } })
  },
  retryJob(jobId) {
    return http.post(`/api/admin/proxy/api/ingest/jobs/${jobId}/retry`)
  },
  jobIssues(jobId) {
    return http.get(`/api/admin/proxy/api/ingest/jobs/${jobId}/issues`)
  },
  retryIssue(issueId) {
    return http.post(`/api/admin/proxy/api/ingest/issues/${issueId}/retry`)
  },
  replaceIssue(issueId, file) {
    const formData = new FormData()
    formData.append('file', file)
    return http.post(`/api/admin/proxy/api/ingest/issues/${issueId}/replace`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  describeIssue(issueId, text) {
    return http.post(`/api/admin/proxy/api/ingest/issues/${issueId}/describe`, { text })
  },
  listDocuments() {
    return http.get('/api/admin/proxy/api/ingest/documents')
  },
  documentStatus(docId) {
    return http.get(`/api/admin/proxy/api/ingest/status/${docId}`)
  },
  documentChunks(docId, limit = 50) {
    return http.get(`/api/admin/proxy/api/ingest/documents/${docId}/chunks`, { params: { limit } })
  },
  deleteDocument(docId) {
    return http.delete(`/api/admin/proxy/api/ingest/documents/${docId}`)
  }
}

// ===== Feedback API（网关注入 user_id）=====
export const feedbackApi = {
  submit(qaLogId, rating, correction = '') {
    return http.post('/api/admin/proxy/api/feedback', { qa_log_id: qaLogId, rating, correction })
  }
}

// ===== Admin API（admin 角色，经网关 proxy）=====
export const adminApi = {
  listPrompts() {
    return http.get('/api/admin/proxy/api/admin/prompts')
  },
  getPrompt(code) {
    return http.get(`/api/admin/proxy/api/admin/prompts/${code}`)
  },
  submitChange(code, newContent) {
    return http.post(`/api/admin/proxy/api/admin/prompts/${code}/change`, { new_content: newContent })
  },
  listApprovals() {
    return http.get('/api/admin/proxy/api/admin/approvals')
  },
  resumeApproval(id, decision) {
    return http.post(`/api/admin/proxy/api/admin/approvals/${id}/resume`, { decision })
  }
}

// ===== Eval API（admin 角色，经网关 proxy）=====
export const evalApi = {
  seed() {
    return http.post('/api/admin/proxy/api/eval/seed')
  },
  run(name = '', engine = 'baseline', withJudge = false) {
    return http.post('/api/admin/proxy/api/eval/run', { name, engine, with_judge: withJudge })
  },
  listRuns() {
    return http.get('/api/admin/proxy/api/eval/runs')
  },
  compare(runA, runB) {
    return http.get('/api/admin/proxy/api/eval/compare', { params: { run_a: runA, run_b: runB } })
  }
}

// ===== Diagnosis API（admin 角色，经网关 proxy）=====
export const diagnosisApi = {
  trigger() {
    return http.post('/api/admin/proxy/api/diagnosis/trigger')
  },
  latest() {
    return http.get('/api/admin/proxy/api/diagnosis/latest')
  },
  metrics() {
    return http.get('/api/admin/proxy/api/diagnosis/metrics')
  }
}