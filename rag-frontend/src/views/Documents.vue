<template>
  <Layout>
    <div class="docs-page">
      <div class="docs-header">
        <h2>文档管理</h2>
        <el-upload :show-file-list="false" :before-upload="handleUpload" accept=".pdf,.md,.docx,.png,.jpg,.jpeg">
          <el-button type="primary">
            <el-icon><Upload /></el-icon> 上传文档
          </el-button>
        </el-upload>
      </div>

      <!-- 上传进度 -->
      <el-alert v-if="uploading" type="info" :closable="false" show-icon>
        <template #title>正在上传并解析：{{ uploadingFile }}...</template>
        <el-progress :percentage="uploadProgress" :status="uploadProgress === 100 ? 'success' : ''" />
      </el-alert>

      <!-- 文档列表 -->
      <el-table :data="documents" stripe style="width: 100%" v-loading="loadingDocs"
                empty-text="暂无文档，请上传">
        <el-table-column prop="filename" label="文件名" min-width="200" />
        <el-table-column prop="doc_type" label="类型" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small">{{ row.doc_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="page_count" label="页数" width="70" align="center" />
        <el-table-column prop="chunk_count" label="分块" width="70" align="center" />
        <el-table-column prop="status" label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="入库时间" width="160">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="center">
          <template #default="{ row }">
            <el-button text size="small" @click="viewChunks(row)">
              <el-icon><View /></el-icon> 分块
            </el-button>
            <el-popconfirm title="确认删除？" @confirm="handleDelete(row.id)">
              <template #reference>
                <el-button text size="small" type="danger">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分块预览弹窗 -->
      <el-dialog v-model="chunksDialog" title="文档分块预览" width="700px" top="5vh">
        <div v-if="currentDoc" class="chunks-preview">
          <p class="doc-info">{{ currentDoc.filename }} · {{ currentDoc.doc_type }} · {{ currentDoc.page_count }}页</p>
          <div v-for="chunk in chunks" :key="chunk.id" class="chunk-card">
            <div class="chunk-meta">
              <el-tag size="small" :type="chunk.chunk_type === 'table' ? 'warning' : chunk.chunk_type === 'image' ? 'success' : 'info'">
                {{ chunk.chunk_type }}
              </el-tag>
              <span>p.{{ chunk.page_no + 1 }} · seq {{ chunk.seq }}</span>
            </div>
            <div class="chunk-content">{{ chunk.content }}</div>
          </div>
        </div>
      </el-dialog>
    </div>
  </Layout>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import Layout from './Layout.vue'
import { ingestApi } from '../api/client'

const documents = ref([])
const loadingDocs = ref(false)
const uploading = ref(false)
const uploadingFile = ref('')
const uploadProgress = ref(0)
const chunksDialog = ref(false)
const chunks = ref([])
const currentDoc = ref(null)

onMounted(loadDocuments)

async function loadDocuments() {
  loadingDocs.value = true
  try {
    const { data } = await ingestApi.listDocuments()
    documents.value = data
  } catch (e) {
    ElMessage.error('加载文档列表失败')
  } finally {
    loadingDocs.value = false
  }
}

async function handleUpload(file) {
  uploading.value = true
  uploadingFile.value = file.name
  uploadProgress.value = 30
  try {
    uploadProgress.value = 60
    const { data } = await ingestApi.upload(file, true)
    uploadProgress.value = 100
    if (data.status === 1) {
      ElMessage.success(`${file.name} 入库成功（${data.chunks} 块）`)
    } else if (data.deduplicated) {
      ElMessage.info(`${file.name} 已存在`)
    }
    await loadDocuments()
  } catch (e) {
    ElMessage.error('上传失败：' + (e.response?.data?.error || e.message))
  } finally {
    uploading.value = false
    uploadProgress.value = 0
  }
  return false  // 阻止 el-upload 默认行为
}

async function handleDelete(docId) {
  try {
    await ingestApi.deleteDocument(docId)
    ElMessage.success('删除成功')
    await loadDocuments()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.error || e.message))
  }
}

async function viewChunks(doc) {
  currentDoc.value = doc
  try {
    const { data } = await ingestApi.documentChunks(doc.id)
    chunks.value = data.chunks || []
    chunksDialog.value = true
  } catch (e) {
    ElMessage.error('加载分块失败')
  }
}

function statusText(status) {
  return { 0: '处理中', 1: '已入库', 2: '失败', 3: '已下线' }[status] || '未知'
}
function statusType(status) {
  return { 0: 'warning', 1: 'success', 2: 'danger', 3: 'info' }[status] || ''
}
function formatDate(dt) {
  if (!dt) return '-'
  return dt.replace('T', ' ').slice(0, 19)
}
</script>

<style scoped>
.docs-page { background: #fff; border-radius: 8px; padding: 24px; border: 1px solid #e4e7ed; }
.docs-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.docs-header h2 { font-size: 18px; color: #303133; }

.chunks-preview { max-height: 70vh; overflow-y: auto; }
.doc-info { color: #909399; font-size: 13px; margin-bottom: 12px; }
.chunk-card {
  border: 1px solid #e4e7ed; border-radius: 8px; padding: 12px; margin-bottom: 8px;
}
.chunk-meta {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
  font-size: 12px; color: #909399;
}
.chunk-content {
  font-size: 13px; line-height: 1.5; white-space: pre-wrap;
  max-height: 200px; overflow-y: auto; color: #303133;
}
</style>
