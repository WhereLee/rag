<template>
  <Layout>
    <div class="ask-layout">
      <!-- 左侧：目录 → 会话 -->
      <aside class="chat-sidebar">
        <div class="sb-section">
          <div class="sb-header">
            <span>目录</span>
          </div>
          <div class="sb-list">
            <div class="sb-item" :class="{ active: currentDir === null }" @click="selectDir(null)">
              <el-icon><Folder /></el-icon><span>全部文件</span>
            </div>
            <div v-for="d in dirs" :key="d.id" class="sb-item"
                 :class="{ active: currentDir === d.id }" @click="selectDir(d.id)">
              <el-icon><Folder /></el-icon><span class="sb-name" :title="d.name">{{ d.name }}</span>
            </div>
            <el-empty v-if="dirs.length === 0" description="还没有目录，可先到「文档管理」整理" :image-size="42" />
          </div>
        </div>

        <div class="sb-section sb-grow" v-if="currentDir !== null">
          <div class="sb-header">
            <span>对话</span>
            <el-button text size="small" type="primary" @click="createSession">
              <el-icon><Plus /></el-icon> 新建
            </el-button>
          </div>
          <div class="sb-list">
            <div v-for="s in sessions" :key="s.session_id" class="sb-item"
                 :class="{ active: currentSession === s.session_id }" @click="selectSession(s.session_id)">
              <el-icon><ChatDotRound /></el-icon>
              <span class="sb-name" :title="s.summary || '未命名对话'">{{ s.summary || '未命名对话' }}</span>
              <span class="sb-turns">{{ s.turns || 0 }}</span>
            </div>
            <div v-if="sessions.length === 0" class="sb-hint">
              在「{{ currentDirName }}」下还没有对话，新建一个开始提问
            </div>
          </div>
        </div>
      </aside>

      <!-- 右侧：聊天区 -->
      <div class="chat-main">
        <div class="chat-header">
          <h2>{{ currentDirName }}<template v-if="currentSession"> · {{ currentSummary }}</template></h2>
        </div>

        <div class="chat-body" ref="bodyRef">
          <div v-if="!currentSession" class="chat-empty">
            <el-empty :description="currentDir === null
              ? '选择或新建一个目录对话：相关文件放进一个目录，在这个目录里连续提问'
              : '在左侧新建一个对话，然后开始提问'" />
          </div>

          <div v-for="m in messages" :key="m.id" class="msg-row" :class="m.role">
            <div class="msg-bubble">
              <div v-if="m.role === 'assistant'" class="msg-meta">
                <el-tag v-if="m.cached && !m.cacheShared" type="success" size="small" effect="light">来自历史回答</el-tag>
                <el-tag v-if="m.lowConfidence" type="warning" size="small" effect="light">低置信度</el-tag>
              </div>
              <div v-if="m.error" class="msg-error">
                <el-alert type="error" :closable="false" show-icon :title="m.error" />
              </div>
              <div v-else-if="m.rejected" class="msg-reject">
                <el-alert type="warning" :closable="false" show-icon
                          title="资料中未找到相关内容" :description="m.rejectReason" />
              </div>
              <div v-else class="msg-text">
                <template v-if="m.streaming && !m.text">生成中…</template>
                <div v-else class="msg-text md-body" v-html="mdRender(m.text)"></div>
              </div>
              <div v-if="m.thinking" class="thinking-box" :class="{ open: m.thinkingOpen }">
                <div class="thinking-head" @click="m.thinkingOpen = !m.thinkingOpen">
                  <el-icon class="th-icon">
                    <component :is="m.thinkingOpen ? 'ArrowDownBold' : 'ArrowRightBold'" />
                  </el-icon>
                  <span class="th-title">{{ thinkingTitle(m) }}</span>
                  <span class="th-toggle">{{ m.thinkingOpen ? '收起' : '展开' }}</span>
                </div>
                <div v-show="m.thinkingOpen" class="thinking-body md-body" v-html="mdRender(m.thinking)"></div>
              </div>
              <div v-if="m.citations && m.citations.length" class="msg-citations">
                <el-tag v-for="c in m.citations" :key="c.index" size="small" effect="plain"
                        class="cite-tag" :title="`相关度 ${c.score}`">
                  [{{ c.index }}] {{ c.doc_name }}{{ c.page_no ? ` 第${c.page_no}页` : '' }}
                  <span class="cite-score">{{ c.score }}</span>
                </el-tag>
              </div>
              <!-- 反馈闭环：回答完成后可点赞/点踩（点踩可附原因，进入 bad case 归因） -->
              <div v-if="m.stopped && m.text" class="msg-stopped">已停止生成（回答不完整，未存入问答存档）</div>
              <div v-if="m.qaLogId && !m.streaming && !m.rejected && !m.error" class="msg-feedback">
                <span v-if="m.feedbackRating === 1" class="fb-done fb-good">已反馈：有帮助</span>
                <span v-else-if="m.feedbackRating === -1" class="fb-done fb-bad">已反馈：没帮助</span>
                <template v-else>
                  <el-button size="small" text type="primary" @click="submitFeedback(m, 1)">有帮助</el-button>
                  <el-button size="small" text type="danger" @click="submitFeedback(m, -1)">没帮助</el-button>
                </template>
              </div>
            </div>
          </div>
        </div>

        <div class="chat-input">
          <el-input v-model="draft" type="textarea" :rows="2" resize="none"
                    :disabled="!currentSession || loading" :placeholder="inputPlaceholder"
                    maxlength="500" show-word-limit
                    @keydown.enter.exact.prevent="send" />
          <div class="input-actions">
            <div class="input-left">
              <el-switch v-model="thinkingMode" size="small" />
              <span class="thinking-label">思考模式</span>
              <span class="input-hint">回车发送；连续提问可追问上一轮内容</span>
            </div>
           <el-button v-if="loading" type="danger" plain @click="stopGenerate">停止生成</el-button>
            <el-button v-else type="primary" :disabled="!currentSession || !draft.trim()"
                       @click="send">发送</el-button>
          </div>
        </div>
      </div>
    </div>
  </Layout>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'
import Layout from './Layout.vue'
import { dirApi, qaApi, sessionApi, feedbackApi } from '../api/client'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

// markdown 渲染 + 引用标注高亮（与旧链路 Chat.vue 同款）；流式中间态也能渲染
function mdRender(text) {
  if (!text) return ''
  return md.render(text).replace(/\[(\d+)\]/g, '<span class="ref-mark">[$1]</span>')
}

const dirs = ref([])
const currentDir = ref(null)
const sessions = ref([])
const currentSession = ref('')
const messages = ref([])
const draft = ref('')
const loading = ref(false)
const thinkingMode = ref(true)   // 思考开关：默认开（忠实度高）；关=省 reasoning token
const bodyRef = ref(null)
let msgSeq = 0
let typewriterTimer = null
let stopController = null   // 停止生成：AbortController 中断当前 SSE 连接

// 打字机效果：一次性到达的文本（缓存命中）按小步长逐渐输出，与流式回答视觉一致
function typewriter(m, fullText) {
  if (typewriterTimer) clearInterval(typewriterTimer)
  let i = 0
  typewriterTimer = setInterval(() => {
    i = Math.min(i + 3, fullText.length)
    m.text = fullText.slice(0, i)
    scrollBottom()
    if (i >= fullText.length) {
      clearInterval(typewriterTimer)
      typewriterTimer = null
    }
  }, 16)
}

onBeforeUnmount(() => { if (typewriterTimer) clearInterval(typewriterTimer) })

const currentDirName = computed(() => {
  if (currentDir.value === null) return '全部文件'
  const d = dirs.value.find(x => x.id === currentDir.value)
  return d ? d.name : '全部文件'
})

const inputPlaceholder = computed(() => {
  if (!currentSession.value) return currentDir.value === null ? '先选择目录并新建对话' : '先新建对话'
  return loading.value ? '生成中…' : '输入问题，回车发送'
})

const currentSummary = computed(() => {
  const s = sessions.value.find(x => x.session_id === currentSession.value)
  return s ? (s.summary || '未命名对话') : ''
})

function thinkingTitle(m) {
  if (!m.thinkingOpen) return '已思考'
  return (m.streaming && !m.text) ? '思考中…' : '思考过程'
}

onMounted(loadDirs)

async function loadDirs() {
  try {
    const { data } = await dirApi.list()
    dirs.value = Array.isArray(data.items) ? data.items : []
  } catch (e) {
    ElMessage.error('加载目录失败')
  }
}

async function selectDir(id) {
  currentDir.value = id
  currentSession.value = ''
  messages.value = []
  sessions.value = []
  if (id !== null) await loadSessions(id)
}

async function loadSessions(dirId) {
  try {
    const { data } = await sessionApi.list(dirId)
    sessions.value = Array.isArray(data.items) ? data.items : []
  } catch (e) {
    ElMessage.error('加载对话失败')
  }
}

async function createSession() {
  if (currentDir.value === null) {
    ElMessage.warning('请先选择一个目录，对话与目录关联')
    return
  }
  const { value } = await ElMessageBox.prompt('给对话起个名字（可选）', '新建对话', {
    confirmButtonText: '创建',
    cancelButtonText: '取消',
    inputPlaceholder: '例如：部署手册答疑'
  }).catch(() => ({}))
  try {
    const { data } = await sessionApi.create(currentDir.value, (value || '').trim())
    await loadSessions(currentDir.value)
    currentSession.value = data.session_id
    messages.value = []
    scrollBottom()
  } catch (e) {
    ElMessage.error('创建对话失败：' + (e.response?.data?.error || e.message))
  }
}

async function selectSession(sid) {
  currentSession.value = sid
  loading.value = false
  try {
    const { data } = await sessionApi.history(sid)
    messages.value = (data.items || []).flatMap(h => [
      { id: 'h' + (++msgSeq), role: 'user', text: h.query, streaming: false },
      { id: 'h' + (++msgSeq), role: 'assistant', text: h.answer, streaming: false }
    ])
    scrollBottom()
  } catch (e) {
    ElMessage.error('加载历史失败')
  }
}

async function send() {
  const query = draft.value.trim()
  if (!query || loading.value || !currentSession.value) return
  draft.value = ''
  loading.value = true
  messages.value.push({ id: 'm' + (++msgSeq), role: 'user', text: query, streaming: false })
  const aid = 'm' + (++msgSeq)
  messages.value.push({ id: aid, role: 'assistant', text: '', streaming: true,
    citations: [], rejected: false, rejectReason: '', error: '', cached: false, lowConfidence: false,
    thinking: '', thinkingOpen: true, cacheShared: false, typewriterMode: false,
    feedbackRating: null, qaLogId: null, stopped: false })
  scrollBottom()
  // 停止生成：每次发送创建新 AbortController（中止后 signal 不可复用）
  stopController = new AbortController()
  try {
    await qaApi.askStream(query, currentSession.value, (evt) => {
      const m = messages.value.find(x => x.id === aid)
      if (!m) return
      if (evt.type === 'meta') {
        m.citations = evt.citations || []
        if (evt.rejected) {
          m.rejected = true
          m.rejectReason = evt.reason || ''
        } else {
          m.lowConfidence = !!evt.low_confidence
          m.cached = !!evt.cached
          m.cacheShared = !!evt.cache_shared
          m.typewriterMode = !!evt.cached  // 缓存命中为一次性整段，走打字机动画
        }
      } else if (evt.type === 'thinking') {
        m.thinking += evt.text || ''
        m.thinkingOpen = true
        scrollBottom()
      } else if (evt.type === 'delta') {
        if (m.typewriterMode) {
          typewriter(m, evt.text || '')
        } else {
          m.text += evt.text || ''
          if (m.thinking) m.thinkingOpen = false  // 正文开始，自动收起思考区
          scrollBottom()
        }
      } else if (evt.type === 'error') {
        m.error = evt.message || '问答失败'
        if (evt.code === 403) {
          ElMessage.error('会话不存在或无权访问')
        }
      } else if (evt.type === 'done') {
        m.streaming = false
        if (m.rejected) m.text = ''
        // 只更新真实 done（带 qa_log_id）；流结束的兜底 done（无 id）不覆盖
        if (evt.qa_log_id) m.qaLogId = evt.qa_log_id
      }
    }, thinkingMode.value, stopController.signal)
  } catch (e) {
    const m = messages.value.find(x => x.id === aid)
    if (e.name === 'AbortError') {
      // 用户主动停止：保留已生成部分，标记停止（loading 由 finally 关闭）
      if (m) {
        m.stopped = true
        m.streaming = false
      }
      ElMessage.info('已停止生成')
    } else if (m) {
      m.error = '请求失败：' + (e.message || '网络错误')
    }
  } finally {
    stopController = null
    loading.value = false
    scrollBottom()
  }
}

async function submitFeedback(m, rating) {
  let correction = ''
  if (rating < 0) {
    // 点踩时可附原因：进入 bad case 自动归因（retrieval/generation）
    try {
      const { value } = await ElMessageBox.prompt('这个回答哪里有问题？（可选）', '反馈', {
        confirmButtonText: '提交',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：答案与文档不符 / 检索到的内容不对 / 回答不完整…',
        inputType: 'textarea'
      })
      correction = value || ''
    } catch { return }  // 用户取消
  }
  try {
    await feedbackApi.submit(m.qaLogId, rating, correction)
    m.feedbackRating = rating
    ElMessage.success(rating > 0 ? '感谢反馈' : '已记录，我们会改进')
  } catch (e) {
    ElMessage.error('反馈提交失败：' + (e?.response?.data?.error || e.message || '网络错误'))
  }
}

function stopGenerate() {
  if (stopController) {
    stopController.abort()
  }
}

function scrollBottom() {  nextTick(() => {
    if (bodyRef.value) bodyRef.value.scrollTop = bodyRef.value.scrollHeight
  })
}
</script>

<style scoped>
.ask-layout { display: flex; gap: 16px; align-items: stretch; height: calc(100vh - 170px); }
.chat-sidebar { width: 250px; flex-shrink: 0; background: #fff; border-radius: 8px;
  border: 1px solid #e4e7ed; padding: 12px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; }
.sb-section { display: flex; flex-direction: column; gap: 6px; }
.sb-grow { flex: 1; min-height: 120px; }
.sb-header { display: flex; justify-content: space-between; align-items: center;
  font-size: 13px; font-weight: 600; color: #303133; }
.sb-list { display: flex; flex-direction: column; gap: 2px; }
.sb-item { display: flex; align-items: center; gap: 6px; padding: 7px 8px; border-radius: 6px;
  cursor: pointer; font-size: 13px; color: #606266; }
.sb-item:hover { background: #f5f7fa; }
.sb-item.active { background: var(--el-color-primary-light-9, #e7f1f0);
  color: var(--el-color-primary, #0f766e); font-weight: 500; }
.sb-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sb-turns { font-size: 11px; color: #909399; }
.sb-hint { font-size: 12px; color: #909399; padding: 8px; line-height: 1.6; }

.chat-main { flex: 1; background: #fff; border-radius: 8px; border: 1px solid #e4e7ed;
  display: flex; flex-direction: column; overflow: hidden; }
.chat-header { padding: 14px 20px; border-bottom: 1px solid #f0f2f5; }
.chat-header h2 { font-size: 16px; color: #303133; }
.chat-body { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
.chat-empty { margin: auto; }
.msg-row { display: flex; }
.msg-row.user { justify-content: flex-end; }
.msg-row.assistant { justify-content: flex-start; }
.msg-bubble { max-width: 78%; padding: 10px 14px; border-radius: 10px; font-size: 14px; line-height: 1.75; }
.msg-row.user .msg-bubble { background: var(--el-color-primary, #0f766e); color: #fff;
  border-top-right-radius: 2px; }
.msg-row.assistant .msg-bubble { background: #f5f7fa; color: #303133; border-top-left-radius: 2px; }
.msg-text { white-space: pre-wrap; word-break: break-word; }
/* markdown 渲染（正文/思考区共用）：覆盖 markdown-it 输出的基础元素 */
.md-body :deep(p) { margin: 0 0 6px; }
.md-body :deep(p:last-child) { margin-bottom: 0; }
.md-body :deep(ul), .md-body :deep(ol) { margin: 4px 0; padding-left: 20px; }
.md-body :deep(li) { margin: 2px 0; }
.md-body :deep(code) { background: #f0f2f5; border-radius: 3px; padding: 1px 4px;
  font-family: Consolas, Monaco, monospace; font-size: 0.92em; }
.md-body :deep(pre) { background: #f6f8fa; border-radius: 6px; padding: 10px;
  overflow-x: auto; margin: 6px 0; }
.md-body :deep(pre code) { background: transparent; padding: 0; }
.md-body :deep(h1), .md-body :deep(h2), .md-body :deep(h3) { margin: 8px 0 4px;
  font-size: 1.05em; font-weight: 600; }
.md-body :deep(blockquote) { margin: 6px 0; padding-left: 10px;
  border-left: 3px solid #e4e7ed; color: #606266; }
.md-body :deep(a) { color: var(--el-color-primary, #0f766e); }
.ref-mark { color: var(--el-color-primary, #0f766e); font-weight: 600; }
.thinking-box { margin-top: 8px; border: 1px solid #e4e7ed; border-radius: 8px;
  background: #fafafa; overflow: hidden; }
.thinking-head { display: flex; align-items: center; gap: 6px; padding: 6px 10px;
  cursor: pointer; user-select: none; color: #909399; font-size: 12px; }
.thinking-head:hover { background: #f0f2f5; }
.th-icon { font-size: 12px; }
.th-title { flex: 1; font-weight: 500; }
.th-toggle { font-size: 11px; }
.thinking-body { max-height: 200px; overflow-y: auto; padding: 4px 10px 10px;
  white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.7;
  color: #606266; border-top: 1px dashed #e4e7ed; }
.msg-meta { margin-bottom: 6px; }
.msg-meta .el-tag { margin-right: 6px; }
.msg-citations { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.msg-feedback { display: flex; align-items: center; gap: 4px; margin-top: 6px; }
.msg-stopped { font-size: 12px; color: #909399; margin-top: 6px; }
.fb-done { font-size: 12px; color: #909399; }
.fb-good { color: #67c23a; }
.fb-bad { color: #f56c6c; }
.cite-tag { cursor: default; }
.cite-score { color: #909399; font-size: 11px; margin-left: 2px; }
.chat-input { padding: 12px 20px 16px; border-top: 1px solid #f0f2f5; }
.input-actions { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.input-left { display: flex; align-items: center; gap: 6px; }
.thinking-label { font-size: 12px; color: #606266; }
.input-hint { font-size: 12px; color: #909399; margin-left: 10px; }
</style>
