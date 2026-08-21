<template>
  <Layout>
    <div class="docs-layout">
      <!-- 左侧目录栏（单层目录，类网盘组织） -->
      <aside class="dir-sidebar">
        <div class="dir-header">
          <span>目录</span>
          <el-button text size="small" type="primary" @click="handleCreateDir">
            <el-icon><FolderAdd /></el-icon> 新建
          </el-button>
        </div>
        <div class="dir-list">
          <div class="dir-item" :class="{ active: currentDir === null }" @click="selectDir(null)">
            <el-icon><Folder /></el-icon>
            <span class="dir-name">全部文件</span>
          </div>
          <div v-for="d in dirs" :key="d.id" class="dir-item"
               :class="{ active: currentDir === d.id }" @click="selectDir(d.id)">
            <el-icon><Folder /></el-icon>
            <span class="dir-name" :title="d.name">{{ d.name }}</span>
            <span class="dir-count">{{ d.file_count || 0 }}</span>
            <span class="dir-ops" @click.stop>
              <el-icon title="重命名" @click="handleRenameDir(d)"><EditPen /></el-icon>
              <el-icon title="删除" @click="handleDeleteDir(d)"><Delete /></el-icon>
            </span>
          </div>
          <el-empty v-if="dirs.length === 0" description="还没有目录" :image-size="48" />
        </div>
      </aside>

      <!-- 右侧内容 -->
      <div class="docs-page">
        <div class="docs-header">
          <h2>{{ currentDirName }}</h2>
          <el-upload :show-file-list="false" :before-upload="handleUpload"
                     accept=".txt,.md,.pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.webp">
            <el-button type="primary" :loading="uploading">
              <el-icon><Upload /></el-icon> 上传文件
            </el-button>
            <template #tip>
              <span v-if="uploadProgress >= 0" class="upload-progress">分片上传中 {{ uploadProgress }}%</span>
            </template>
          </el-upload>
        </div>

        <el-tabs v-model="activeTab">
          <el-tab-pane label="我的文件" name="files">
            <el-table :data="files" stripe style="width: 100%" v-loading="loading"
                      empty-text="还没有文件，上传第一份文档开始问答">
              <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
              <el-table-column label="大小" width="90" align="center">
                <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
              </el-table-column>
              <el-table-column label="类型" width="100" align="center">
                <template #default="{ row }">{{ extLabel(row.ext) }}</template>
              </el-table-column>
              <el-table-column label="上传时间" width="160" align="center">
                <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="解析状态" width="100" align="center">
                <template #default="{ row }">
                  <el-tag :type="parseTagType(row.parse_status)" size="small" effect="light">
                    {{ parseLabel(row.parse_status) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="300" align="center">
                <template #default="{ row }">
                  <el-button text size="small" type="primary" @click="handleDownload(row)">
                    <el-icon><Download /></el-icon> 下载
                  </el-button>
                  <el-button text size="small" type="primary" :disabled="!canPreview(row)" @click="handlePreview(row)">
                    <el-icon><View /></el-icon> 预览
                  </el-button>
                  <el-button text size="small" @click="handleRename(row)">
                    <el-icon><EditPen /></el-icon> 重命名
                  </el-button>
                  <el-dropdown trigger="click" @command="cmd => handleMove(row, cmd)">
                    <el-button text size="small">
                      <el-icon><FolderOpened /></el-icon> 移动
                    </el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item :command="null" :disabled="currentDir === null">
                          根目录
                        </el-dropdown-item>
                        <el-dropdown-item v-for="d in dirs" :key="d.id" :command="d.id"
                                          :disabled="currentDir === d.id">
                          {{ d.name }}
                        </el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                  <el-button v-if="canReparse(row)" text size="small" type="warning" @click="handleReparse(row)">
                    <el-icon><RefreshRight /></el-icon> 重新解析
                  </el-button>
                  <el-popconfirm title="删除后进入回收站，可恢复" @confirm="handleDelete(row)">
                    <template #reference>
                      <el-button text size="small" type="danger">
                        <el-icon><Delete /></el-icon> 删除
                      </el-button>
                    </template>
                  </el-popconfirm>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="total > pageSize" class="pagination-wrap">
              <el-pagination background layout="total, prev, pager, next" :total="total"
                             :current-page="page" :page-size="pageSize" @current-change="onPageChange" />
            </div>
          </el-tab-pane>

          <el-tab-pane label="回收站" name="trash">
            <el-table :data="trashFiles" stripe style="width: 100%" v-loading="trashLoading"
                      empty-text="回收站是空的">
              <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip />
              <el-table-column label="大小" width="100" align="center">
                <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
              </el-table-column>
              <el-table-column label="类型" width="110" align="center">
                <template #default="{ row }">{{ extLabel(row.ext) }}</template>
              </el-table-column>
              <el-table-column label="删除时间" width="170" align="center">
                <template #default="{ row }">{{ formatDate(row.deleted_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="120" align="center">
                <template #default="{ row }">
                  <el-button text size="small" type="primary" @click="handleRestore(row)">
                    <el-icon><RefreshLeft /></el-icon> 恢复
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>

        <el-dialog v-model="previewVisible" title="在线预览" width="60%" top="6vh">
          <div v-if="previewText" class="preview-body">{{ previewText }}</div>
          <el-empty v-else description="暂无预览内容" />
        </el-dialog>
      </div>
    </div>
  </Layout>
</template>

<script setup>
import { ref, computed, onMounted, watch, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import Layout from './Layout.vue'
import { fileApi, dirApi } from '../api/client'

const activeTab = ref('files')
const files = ref([])
const loading = ref(false)
const uploading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const trashFiles = ref([])
const trashLoading = ref(false)
const previewVisible = ref(false)
const previewText = ref('')
const dirs = ref([])
const currentDir = ref(null)
const uploadProgress = ref(-1)   // -1=不显示；0-100=分片上传进度
let pollTimer = null              // 解析状态轮询：有 pending/parsing 文件时定时刷新

const currentDirName = computed(() => {
  if (currentDir.value === null) return '全部文件'
  const d = dirs.value.find(x => x.id === currentDir.value)
  return d ? d.name : '全部文件'
})

const PARSE_LABELS = {
  pending: '待解析', parsing: '解析中', success: '已解析', partial: '部分失败', failed: '失败'
}

function parseLabel(s) {
  return PARSE_LABELS[s] || '待解析'
}

function parseTagType(s) {
  return ({ pending: 'info', parsing: 'warning', success: 'success', partial: 'danger', failed: 'danger' })[s] || 'info'
}

function canPreview(row) {
  return row.parse_status === 'success' || row.parse_status === 'partial'
}

function canReparse(row) {
  return row.parse_status === 'failed' || row.parse_status === 'partial'
}

const EXT_LABELS = {
  '.pdf': 'PDF 文档', '.docx': 'Word 文档', '.xlsx': 'Excel 表格', '.pptx': 'PPT 演示',
  '.md': 'Markdown', '.txt': '文本文件', '.png': '图片', '.jpg': '图片', '.jpeg': '图片', '.gif': '图片'
}

function extLabel(ext) {
  return EXT_LABELS[ext] || (ext ? ext.toUpperCase().replace('.', '') + ' 文件' : '文件')
}

onMounted(() => { loadDirs(); loadFiles() })
watch(activeTab, (t) => { if (t === 'trash') loadTrash() })
onBeforeUnmount(() => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } })

async function loadDirs() {
  try {
    const { data } = await dirApi.list()
    dirs.value = Array.isArray(data.items) ? data.items : []
  } catch (e) {
    ElMessage.error('加载目录失败')
  }
}

function selectDir(id) {
  currentDir.value = id
  page.value = 1
  loadFiles()
}

async function loadFiles() {
  loading.value = true
  try {
    const { data } = await fileApi.list(page.value, pageSize.value, currentDir.value)
    files.value = Array.isArray(data.items) ? data.items : []
    total.value = data.total || 0
  } catch (e) {
    ElMessage.error('加载文件列表失败')
  } finally {
    loading.value = false
  }
  startParsePolling()
}

// 解析状态轮询：列表存在待解析/解析中的文件时每 3s 静默刷新，直到全部完成或超时（2 分钟）
function startParsePolling() {
  const hasUnfinished = files.value.some(f => f.parse_status === 'pending' || f.parse_status === 'parsing')
  if (!hasUnfinished) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
    return
  }
  if (pollTimer) return
  let tries = 0
  pollTimer = setInterval(async () => {
    tries++
    try {
      const { data } = await fileApi.list(page.value, pageSize.value, currentDir.value)
      files.value = Array.isArray(data.items) ? data.items : []
      total.value = data.total || 0
    } catch (e) {
      // 静默：轮询失败不打扰用户，下轮重试
    }
    const stillUnfinished = files.value.some(f => f.parse_status === 'pending' || f.parse_status === 'parsing')
    if (!stillUnfinished || tries >= 40) {
      clearInterval(pollTimer); pollTimer = null
    }
  }, 3000)
}

async function loadTrash() {
  trashLoading.value = true
  try {
    const { data } = await fileApi.trash()
    trashFiles.value = Array.isArray(data) ? data : []
  } catch (e) {
    ElMessage.error('加载回收站失败')
  } finally {
    trashLoading.value = false
  }
}

function onPageChange(p) {
  page.value = p
  loadFiles()
}

async function handleCreateDir() {
  const { value } = await ElMessageBox.prompt('请输入目录名', '新建目录', {
    confirmButtonText: '创建',
    cancelButtonText: '取消',
    inputPattern: /\S+/,
    inputErrorMessage: '目录名不能为空'
  }).catch(() => ({}))
  if (!value || value.trim() === '') return
  try {
    await dirApi.create(value.trim())
    ElMessage.success('目录创建成功')
    await loadDirs()
  } catch (e) {
    ElMessage.error('创建失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleRenameDir(dir) {
  const { value } = await ElMessageBox.prompt('请输入新目录名', '重命名目录', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    inputValue: dir.name,
    inputPattern: /\S+/,
    inputErrorMessage: '目录名不能为空'
  }).catch(() => ({}))
  if (!value || value.trim() === '') return
  try {
    await dirApi.rename(dir.id, value.trim())
    ElMessage.success('重命名成功')
    await loadDirs()
  } catch (e) {
    ElMessage.error('重命名失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleDeleteDir(dir) {
  try {
    await ElMessageBox.confirm(`确定删除目录「${dir.name}」？非空目录无法删除`, '删除目录', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch (e) {
    return
  }
  try {
    await dirApi.remove(dir.id)
    ElMessage.success('目录已删除')
    if (currentDir.value === dir.id) selectDir(null)
    await loadDirs()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleMove(row, dirId) {
  if (dirId === undefined) return
  try {
    await fileApi.move(row.id, dirId)
    ElMessage.success('移动成功')
    await loadFiles()
  } catch (e) {
    ElMessage.error('移动失败：' + (e.response?.data?.error || e.message))
  }
}

// 秒传流程：算 sha256 → check-hash 命中直接成功（不传字节），未命中走整传或分片
const CHUNK_SIZE = 5 * 1024 * 1024      // 分片大小 5MB
const CHUNK_THRESHOLD = 20 * 1024 * 1024 // 超过 20MB 走分片上传

// 支持上传的扩展名白名单（与 Java 网关校验一致，解析域支持范围）
const ALLOWED_EXTS = ['txt', 'md', 'pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpg', 'jpeg', 'webp']

async function handleUpload(file) {
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  if (!ALLOWED_EXTS.includes(ext)) {
    ElMessage.error(`不支持的文件类型 .${ext}（支持：${ALLOWED_EXTS.join(', ')}）`)
    return false
  }
  uploading.value = true
  uploadProgress.value = 0
  try {
    const hash = await sha256(file)
    const chk = await fileApi.checkHash(hash, file.size, file.name)
    if (chk.data.hit) {
      ElMessage.success(`「${file.name}」秒传成功（内容已存在）`)
      await loadFiles()
      return false
    }
    const data = file.size > CHUNK_THRESHOLD
      ? await uploadChunked(file, hash)
      : (await fileApi.upload(file, currentDir.value)).data
    if (data.duplicate_name) {
      ElMessage.warning(`「${file.name}」上传成功（已存在同名文件）`)
    } else {
      ElMessage.success(`「${file.name}」上传成功`)
    }
    await loadFiles()
  } catch (e) {
    ElMessage.error('上传失败：' + (e.response?.data?.error || e.message))
  } finally {
    uploading.value = false
    uploadProgress.value = -1
  }
  return false  // 阻止 el-upload 默认行为
}

// 分片上传：init → 串行传片（断点续传跳过已传分片）→ complete；逐片更新进度
async function uploadChunked(file, hash) {
  const chunkCount = Math.ceil(file.size / CHUNK_SIZE)
  const init = await fileApi.uploadInit(hash, file.size, file.name, chunkCount, CHUNK_SIZE, currentDir.value)
  if (init.data.hit) {
    return init.data
  }
  const uploadId = init.data.upload_id
  const st = await fileApi.uploadStatus(uploadId)
  const uploaded = new Set(st.data.uploaded || [])
  for (let i = 0; i < chunkCount; i++) {
    if (uploaded.has(i)) continue
    const blob = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size))
    await fileApi.uploadChunk(uploadId, i, blob)
    uploadProgress.value = Math.round(((i + 1) / chunkCount) * 100)
  }
  const done = await fileApi.uploadComplete(uploadId)
  uploadProgress.value = 100
  return done.data
}

// 浏览器内置 Web Crypto 计算文件 sha256（localhost 属安全上下文，可用）
async function sha256(file) {
  const buf = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buf)
  return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('')
}

async function handleDownload(row) {
  try {
    await fileApi.download(row.id, row.filename)
  } catch (e) {
    ElMessage.error('下载失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleRename(row) {
  const { value } = await ElMessageBox.prompt('请输入新文件名', '重命名', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    inputValue: row.filename,
    inputPattern: /\S+/,
    inputErrorMessage: '文件名不能为空'
  }).catch(() => ({}))
  if (!value || value.trim() === '') return
  try {
    await fileApi.rename(row.id, value.trim())
    ElMessage.success('重命名成功')
    await loadFiles()
  } catch (e) {
    ElMessage.error('重命名失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleReparse(row) {
  try {
    await fileApi.reparse(row.id)
    ElMessage.success('已加入解析队列')
    await loadFiles()
  } catch (e) {
    ElMessage.error('重新解析失败：' + (e.response?.data?.error || e.message))
  }
}

async function handlePreview(row) {
  try {
    const { data } = await fileApi.preview(row.id)
    if (!data.previewable) {
      ElMessage.warning(data.reason || '暂无法预览')
      return
    }
    previewText.value = data.text || ''
    previewVisible.value = true
  } catch (e) {
    ElMessage.error('预览失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleDelete(row) {
  try {
    const { data } = await fileApi.remove(row.id)
    ElMessage.success(data.warning ? '已移入回收站（物理删除待清理）' : '已移入回收站')
    await loadFiles()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.error || e.message))
  }
}

async function handleRestore(row) {
  try {
    await fileApi.restore(row.id)
    ElMessage.success('恢复成功')
    await loadTrash()
    await loadFiles()
  } catch (e) {
    ElMessage.error('恢复失败：' + (e.response?.data?.error || e.message))
  }
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

function formatDate(dt) {
  if (!dt) return '-'
  return String(dt).replace('T', ' ').slice(0, 19)
}
</script>

<style scoped>
.docs-layout { display: flex; gap: 16px; align-items: flex-start; }
.dir-sidebar { width: 220px; flex-shrink: 0; background: #fff; border-radius: 8px;
  border: 1px solid #e4e7ed; padding: 12px; }
.dir-header { display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; font-size: 14px; font-weight: 600; color: #303133; }
.dir-list { display: flex; flex-direction: column; gap: 2px; }
.dir-item { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-radius: 6px;
  cursor: pointer; font-size: 13px; color: #606266; }
.dir-item:hover { background: #f5f7fa; }
.dir-item.active { background: var(--el-color-primary-light-9, #e7f1f0); color: var(--el-color-primary, #0f766e);
  font-weight: 500; }
.dir-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dir-count { font-size: 12px; color: #909399; }
.dir-ops { display: none; gap: 4px; color: #909399; }
.dir-item:hover .dir-ops { display: inline-flex; }
.dir-ops .el-icon { cursor: pointer; }
.dir-ops .el-icon:hover { color: var(--el-color-primary, #0f766e); }
.docs-page { flex: 1; background: #fff; border-radius: 8px; padding: 24px; border: 1px solid #e4e7ed; }
.docs-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.docs-header h2 { font-size: 18px; color: #303133; }
.upload-progress { margin-left: 10px; font-size: 12px; color: #909399; }
.pagination-wrap { display: flex; justify-content: flex-end; margin-top: 16px; }
.preview-body { max-height: 65vh; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  font-size: 14px; line-height: 1.8; color: #303133; }
</style>
