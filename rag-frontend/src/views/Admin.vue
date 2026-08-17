<template>
  <Layout>
    <div class="admin-page">
      <el-tabs v-model="activeTab">
        <!-- Prompt 管理 -->
        <el-tab-pane label="Prompt 管理" name="prompts">
          <el-table :data="prompts" stripe v-loading="loadingPrompts">
            <el-table-column prop="code" label="编码" width="120" />
            <el-table-column prop="version" label="版本" width="70" align="center" />
            <el-table-column prop="content" label="内容" min-width="300">
              <template #default="{ row }">
                <div class="prompt-preview">{{ row.content?.slice(0, 120) }}...</div>
              </template>
            </el-table-column>
            <el-table-column prop="updated_at" label="更新时间" width="160">
              <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center">
              <template #default="{ row }">
                <el-button text size="small" @click="editPrompt(row)">编辑</el-button>
              </template>
            </el-table-column>
          </el-table>

          <!-- 编辑弹窗 -->
          <el-dialog v-model="editDialog" title="编辑 Prompt" width="700px">
            <p style="margin-bottom:8px; color:#909399; font-size:13px">
              编码：{{ editCode }} · 当前版本：{{ editVersion }}
            </p>
            <el-input v-model="editContent" type="textarea" :rows="12" />
            <template #footer>
              <el-button @click="editDialog = false">取消</el-button>
              <el-button type="warning" :loading="submitting" @click="submitPromptChange">
                提交变更（需审批）
              </el-button>
            </template>
          </el-dialog>
        </el-tab-pane>

        <!-- 审批 -->
        <el-tab-pane label="审批" name="approvals">
          <el-button @click="loadApprovals" style="margin-bottom:12px">
            <el-icon><Refresh /></el-icon> 刷新
          </el-button>
          <el-table :data="approvals" stripe>
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="prompt_code" label="Prompt" width="120" />
            <el-table-column label="变更" min-width="200">
              <template #default="{ row }">
                <span class="change-preview">
                  {{ row.old_content?.slice(0, 60) }}... → {{ row.new_content?.slice(0, 60) }}...
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="decision" label="状态" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="decisionType(row.decision)" size="small">{{ decisionText(row.decision) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="180" align="center">
              <template #default="{ row }">
                <template v-if="row.decision === 'pending'">
                  <el-button text size="small" type="success" @click="handleApproval(row.id, 'approved')">批准</el-button>
                  <el-button text size="small" type="danger" @click="handleApproval(row.id, 'rejected')">拒绝</el-button>
                </template>
                <span v-else class="decided-at">{{ formatDate(row.decided_at) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 评估 -->
        <el-tab-pane label="评估" name="eval">
          <div style="margin-bottom:16px; display:flex; gap:8px">
            <el-button @click="runEval('baseline')" :loading="evalRunning === 'baseline'">跑基线评估</el-button>
            <el-button @click="runEval('agent')" :loading="evalRunning === 'agent'">跑 Agent 评估</el-button>
            <el-button @click="loadEvalRuns">
              <el-icon><Refresh /></el-icon>
            </el-button>
          </div>
          <el-table :data="evalRuns" stripe>
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="name" label="名称" width="160" />
            <el-table-column label="Recall" width="80" align="center">
              <template #default="{ row }">{{ row.metrics?.context_recall ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="MRR" width="80" align="center">
              <template #default="{ row }">{{ row.metrics?.mrr ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="Refuse" width="80" align="center">
              <template #default="{ row }">{{ row.metrics?.refuse_accuracy ?? '-' }}</template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" width="160">
              <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 诊断 -->
        <el-tab-pane label="诊断" name="diagnosis">
          <div style="margin-bottom:16px; display:flex; gap:8px">
            <el-button @click="triggerDiagnosis" :loading="diagnosing">触发诊断</el-button>
            <el-button @click="loadDiagnosis">
              <el-icon><Refresh /></el-icon>
            </el-button>
          </div>
          <div v-if="diagnosisReport" class="diagnosis-report">
            <div v-if="diagnosisReport.summary" class="report-section">
              <h4>摘要</h4>
              <p>{{ diagnosisReport.summary }}</p>
            </div>
            <div v-if="diagnosisReport.suggestions?.length" class="report-section">
              <h4>建议</h4>
              <ul>
                <li v-for="(s, i) in diagnosisReport.suggestions" :key="i">{{ s }}</li>
              </ul>
            </div>
            <div v-if="diagnosisReport.note" class="report-section">
              <p style="color:#909399">{{ diagnosisReport.note }}</p>
            </div>
          </div>
          <el-empty v-else description="暂无诊断报告" />
        </el-tab-pane>
      </el-tabs>
    </div>
  </Layout>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import Layout from './Layout.vue'
import { adminApi, evalApi, diagnosisApi } from '../api/client'

const activeTab = ref('prompts')

// ===== Prompt =====
const prompts = ref([])
const loadingPrompts = ref(false)
const editDialog = ref(false)
const editCode = ref('')
const editVersion = ref(1)
const editContent = ref('')
const submitting = ref(false)

async function loadPrompts() {
  loadingPrompts.value = true
  try {
    const { data } = await adminApi.listPrompts()
    prompts.value = data
  } catch (e) {
    ElMessage.error('加载 Prompt 列表失败')
  } finally {
    loadingPrompts.value = false
  }
}

function editPrompt(row) {
  editCode.value = row.code
  editVersion.value = row.version
  editContent.value = row.content
  editDialog.value = true
}

async function submitPromptChange() {
  submitting.value = true
  try {
    await adminApi.submitChange(editCode.value, editContent.value)
    ElMessage.success('变更已提交，等待审批')
    editDialog.value = false
    await loadApprovals()
  } catch (e) {
    ElMessage.error('提交失败：' + (e.response?.data?.error || e.message))
  } finally {
    submitting.value = false
  }
}

// ===== 审批 =====
const approvals = ref([])

async function loadApprovals() {
  try {
    const { data } = await adminApi.listApprovals()
    approvals.value = data
  } catch (e) {
    ElMessage.error('加载审批列表失败')
  }
}

async function handleApproval(id, decision) {
  try {
    await adminApi.resumeApproval(id, decision)
    ElMessage.success(decision === 'approved' ? '已批准' : '已拒绝')
    await loadApprovals()
    await loadPrompts()
  } catch (e) {
    ElMessage.error('操作失败')
  }
}

function decisionText(d) { return { pending: '待审批', approved: '已批准', rejected: '已拒绝' }[d] || d }
function decisionType(d) { return { pending: 'warning', approved: 'success', rejected: 'danger' }[d] || '' }

// ===== 评估 =====
const evalRuns = ref([])
const evalRunning = ref('')

async function loadEvalRuns() {
  try {
    const { data } = await evalApi.listRuns()
    evalRuns.value = data
  } catch (e) {
    ElMessage.error('加载评估列表失败')
  }
}

async function runEval(engine) {
  evalRunning.value = engine
  try {
    const { data } = await evalApi.run(`${engine}-run`, engine)
    ElMessage.success(`评估完成: MRR=${data.metrics?.mrr}`)
    await loadEvalRuns()
  } catch (e) {
    ElMessage.error('评估失败：' + (e.response?.data?.error || e.message))
  } finally {
    evalRunning.value = ''
  }
}

// ===== 诊断 =====
const diagnosing = ref(false)
const diagnosisReport = ref(null)

async function triggerDiagnosis() {
  diagnosing.value = true
  try {
    await diagnosisApi.trigger()
    ElMessage.success('诊断完成')
    await loadDiagnosis()
  } catch (e) {
    ElMessage.error('诊断失败')
  } finally {
    diagnosing.value = false
  }
}

async function loadDiagnosis() {
  try {
    const { data } = await diagnosisApi.latest()
    diagnosisReport.value = data
  } catch (e) {
    // ignore
  }
}

// ===== 公共 =====
function formatDate(dt) {
  if (!dt) return '-'
  return String(dt).replace('T', ' ').slice(0, 19)
}

onMounted(() => {
  loadPrompts()
  loadApprovals()
  loadEvalRuns()
  loadDiagnosis()
})
</script>

<style scoped>
.admin-page { background: #fff; border-radius: 8px; padding: 24px; border: 1px solid #e4e7ed; }
.prompt-preview { font-size: 12px; color: #606266; white-space: pre-wrap; max-height: 60px; overflow: hidden; }
.change-preview { font-size: 12px; color: #909399; }
.decided-at { font-size: 12px; color: #909399; }

.diagnosis-report { border: 1px solid #e4e7ed; border-radius: 8px; padding: 20px; }
.report-section { margin-bottom: 16px; }
.report-section h4 { margin-bottom: 8px; color: #303133; }
.report-section p { color: #606266; font-size: 14px; line-height: 1.6; }
.report-section ul { padding-left: 20px; color: #606266; }
.report-section li { margin-bottom: 4px; }
</style>
