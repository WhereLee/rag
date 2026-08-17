<template>
  <Layout>
    <div class="chat-page">
      <!-- 会话历史侧边栏 -->
      <div class="chat-sidebar">
        <div class="sidebar-header">
          <span>会话历史</span>
          <el-button text size="small" @click="newSession">
            <el-icon><Plus /></el-icon> 新会话
          </el-button>
        </div>
        <div class="session-list">
          <div v-for="s in sessions" :key="s.id"
               class="session-item" :class="{ active: s.id === currentSession }"
               @click="switchSession(s.id)">
            <span class="session-title">{{ s.title }}</span>
            <span class="session-time">{{ s.time }}</span>
          </div>
          <div v-if="!sessions.length" class="no-sessions">暂无会话</div>
        </div>
      </div>

      <!-- 聊天主体 -->
      <div class="chat-main">
        <div class="messages" ref="messagesRef">
          <div v-if="!messages.length" class="welcome">
            <el-icon :size="48" color="#c0c4cc"><ChatDotRound /></el-icon>
            <p>上传文档后，在这里提问</p>
          </div>
          <div v-for="(msg, i) in messages" :key="i" class="message" :class="msg.role">
            <div class="msg-avatar">
              <el-icon v-if="msg.role === 'user'"><User /></el-icon>
              <el-icon v-else><Monitor /></el-icon>
            </div>
            <div class="msg-content">
              <div class="msg-text" v-html="renderMarkdown(msg.content)"></div>
              <!-- 引用标注 -->
              <div v-if="msg.citations?.length" class="citations">
                <el-tag v-for="c in msg.citations" :key="c.index" size="small" type="info"
                        class="citation-tag">
                  [{{ c.index }}] {{ c.doc_name }} p.{{ c.page_no }}
                </el-tag>
              </div>
              <!-- 反馈按钮 -->
              <div v-if="msg.role === 'assistant' && !msg.loading && msg.qaLogId" class="msg-feedback">
                <el-button text size="small" :type="msg.rated === 1 ? 'success' : ''" @click="rate(i, 1)">
                  <el-icon><Top /></el-icon>
                </el-button>
                <el-button text size="small" :type="msg.rated === -1 ? 'danger' : ''" @click="rate(i, -1)">
                  <el-icon><Bottom /></el-icon>
                </el-button>
              </div>
              <!-- 加载动画 -->
              <div v-if="msg.loading" class="msg-loading">
                <span class="dot"></span><span class="dot"></span><span class="dot"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- 输入区域 -->
        <div class="chat-input">
          <el-input v-model="inputText" type="textarea" :rows="2" placeholder="输入问题，Enter 发送..."
                    :disabled="isStreaming" @keydown.enter.exact="handleSend" resize="none" />
          <el-button type="primary" :loading="isStreaming" :disabled="!inputText.trim()" @click="handleSend">
            {{ isStreaming ? '生成中...' : '发送' }}
          </el-button>
        </div>
      </div>
    </div>
  </Layout>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import Layout from './Layout.vue'
import { chatApi, feedbackApi } from '../api/client'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const messages = ref([])
const sessions = ref([])
const currentSession = ref('')
const inputText = ref('')
const isStreaming = ref(false)
const messagesRef = ref()

function renderMarkdown(text) {
  if (!text) return ''
  // 高亮引用标注 [n]
  let html = md.render(text)
  html = html.replace(/\[(\d+)\]/g, '<span class="ref-mark">[$1]</span>')
  return html
}

function newSession() {
  currentSession.value = ''
  messages.value = []
}

function switchSession(sid) {
  currentSession.value = sid
  loadHistory(sid)
}

async function loadHistory(sid) {
  try {
    const { data } = await chatApi.history(sid)
    messages.value = data.map(d => ({
      role: 'user',
      content: d.query
    }))
    // 简单重建，answer 作为 assistant
    data.forEach(d => {
      messages.value.push({ role: 'assistant', content: d.answer })
    })
  } catch (e) {
    ElMessage.error('加载历史失败')
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (messagesRef.value) {
      messagesRef.value.scrollTop = messagesRef.value.scrollHeight
    }
  })
}

async function handleSend(e) {
  if (e?.shiftKey) return  // Shift+Enter 换行
  e?.preventDefault?.()
  const query = inputText.value.trim()
  if (!query || isStreaming.value) return

  inputText.value = ''
  messages.value.push({ role: 'user', content: query })
  scrollToBottom()

  // 添加 assistant 占位
  const assistantMsg = { role: 'assistant', content: '', loading: true, citations: [] }
  messages.value.push(assistantMsg)
  scrollToBottom()

  isStreaming.value = true
  let sessionId = currentSession.value

  try {
    await chatApi.askStream(query, sessionId, (chunk) => {
      if (chunk.type === 'citations') {
        assistantMsg.citations = chunk.citations || []
        sessionId = chunk.session_id || sessionId
        currentSession.value = sessionId
      } else if (chunk.type === 'delta') {
        assistantMsg.content += chunk.text || ''
        scrollToBottom()
      } else if (chunk.type === 'done') {
        assistantMsg.loading = false
        assistantMsg.totalMs = chunk.total_ms
      }
    })

    // 添加到会话列表
    if (!sessions.value.find(s => s.id === currentSession.value)) {
      sessions.value.unshift({
        id: currentSession.value,
        title: query.slice(0, 30) + (query.length > 30 ? '...' : ''),
        time: new Date().toLocaleTimeString()
      })
    }
  } catch (e) {
    assistantMsg.content = '请求失败：' + (e.message || e)
    assistantMsg.loading = false
    ElMessage.error('问答请求失败')
  } finally {
    isStreaming.value = false
    scrollToBottom()
  }
}

async function rate(msgIndex, rating) {
  const msg = messages.value[msgIndex]
  if (msg.rated) {
    ElMessage.warning('已评价过')
    return
  }
  // TODO: 需要从 qa_log_id 获取（当前流式接口未返回）
  msg.rated = rating
  ElMessage.success(rating === 1 ? '感谢反馈' : '已记录，我们会改进')
}
</script>

<style scoped>
.chat-page { display: flex; height: calc(100vh - 40px); gap: 0; }

/* 侧边栏 */
.chat-sidebar {
  width: 240px; background: #fff; border-radius: 8px; border: 1px solid #e4e7ed;
  display: flex; flex-direction: column; overflow: hidden;
}
.sidebar-header {
  padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;
  border-bottom: 1px solid #e4e7ed; font-size: 14px; font-weight: 500;
}
.session-list { flex: 1; overflow-y: auto; padding: 8px; }
.session-item {
  padding: 10px 12px; border-radius: 6px; cursor: pointer; margin-bottom: 4px;
  display: flex; flex-direction: column; gap: 2px;
}
.session-item:hover { background: #f5f7fa; }
.session-item.active { background: #ecf5ff; color: #409eff; }
.session-title { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-time { font-size: 11px; color: #909399; }
.no-sessions { text-align: center; color: #c0c4cc; padding: 20px; font-size: 13px; }

/* 聊天主体 */
.chat-main {
  flex: 1; display: flex; flex-direction: column; background: #fff; border-radius: 8px;
  border: 1px solid #e4e7ed; overflow: hidden;
}
.messages {
  flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px;
}
.welcome {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  color: #c0c4cc; gap: 12px;
}
.message {
  display: flex; gap: 12px; max-width: 85%;
}
.message.user { align-self: flex-end; flex-direction: row-reverse; }
.message.assistant { align-self: flex-start; }
.msg-avatar {
  width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  background: #ecf5ff; color: #409eff; flex-shrink: 0;
}
.message.user .msg-avatar { background: #409eff; color: white; }
.msg-content { display: flex; flex-direction: column; gap: 6px; }
.msg-text {
  padding: 12px 16px; border-radius: 12px; line-height: 1.6; font-size: 14px;
  background: #f4f4f5;
}
.message.user .msg-text { background: #ecf5ff; }
.msg-text :deep(.ref-mark) {
  background: #409eff20; color: #409eff; padding: 1px 4px; border-radius: 3px; font-weight: 500;
}
.citations { display: flex; flex-wrap: wrap; gap: 4px; }
.citation-tag { font-size: 11px !important; }
.msg-feedback { display: flex; gap: 4px; }
.msg-loading { display: flex; gap: 4px; padding: 8px 0; }
.dot {
  width: 6px; height: 6px; border-radius: 50%; background: #c0c4cc;
  animation: bounce 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

/* 输入区域 */
.chat-input {
  padding: 12px 16px; border-top: 1px solid #e4e7ed;
  display: flex; gap: 8px; align-items: flex-end;
}
.chat-input :deep(.el-textarea__inner) { border-radius: 8px; }
</style>
