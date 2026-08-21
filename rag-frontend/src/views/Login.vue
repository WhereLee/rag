<template>
  <div class="login-wrap">
    <div class="login-brand">
      <div class="brand-top">
        <el-icon :size="26" class="brand-mark"><Document /></el-icon>
        <span class="brand-name">文档问答</span>
      </div>
      <div class="brand-mid">
        <h1>给文档一个能回答问题的入口</h1>
        <p>上传资料，系统解析入库后即可提问；回答逐句标注来源文件与页码，可回溯核对。</p>
        <ul class="brand-points">
          <li>支持 PDF / Word / Excel / PPT / Markdown，自动解析入库</li>
          <li>向量 + 关键词混合检索，粗筛后交叉编码精排</li>
          <li>资料中没有答案时明确拒答，不编造</li>
        </ul>
      </div>
      <div class="brand-foot">蓝图数字研究院 · 内部知识库</div>
    </div>

    <div class="login-panel">
      <el-tabs v-model="activeTab" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form :model="loginForm" :rules="loginRules" ref="loginFormRef" @submit.prevent="handleLogin">
            <el-form-item prop="username">
              <el-input v-model="loginForm.username" placeholder="用户名" prefix-icon="User" size="large" />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="loginForm.password" type="password" placeholder="密码" prefix-icon="Lock"
                        size="large" show-password @keyup.enter="handleLogin" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="large" :loading="loading" @click="handleLogin" style="width:100%">
                登录
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form :model="registerForm" :rules="registerRules" ref="registerFormRef" @submit.prevent="handleRegister">
            <el-form-item prop="username">
              <el-input v-model="registerForm.username" placeholder="用户名（至少2位）" prefix-icon="User" size="large" />
            </el-form-item>
            <el-form-item prop="password">
              <el-input v-model="registerForm.password" type="password" placeholder="密码（至少6位）" prefix-icon="Lock"
                        size="large" show-password />
            </el-form-item>
            <el-form-item prop="confirmPassword">
              <el-input v-model="registerForm.confirmPassword" type="password" placeholder="确认密码" prefix-icon="Lock"
                        size="large" show-password @keyup.enter="handleRegister" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="large" :loading="loading" @click="handleRegister" style="width:100%">
                注册
              </el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authApi } from '../api/client'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const activeTab = ref('login')
const loading = ref(false)
const loginFormRef = ref()
const registerFormRef = ref()

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ username: '', password: '', confirmPassword: '' })

const loginRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const validateConfirm = (rule, value, callback) => {
  if (value !== registerForm.password) {
    callback(new Error('两次密码不一致'))
  } else {
    callback()
  }
}

const registerRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_]{2,32}$/, message: '仅允许字母、数字、下划线，2-32位', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9@#$%^&*._-]{8,32}$/, message: '密码需为8-32位字母、数字或常见符号（@#$%^&*._-）', trigger: 'blur' }
  ],
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    { validator: validateConfirm, trigger: 'blur' }
  ]
}

async function handleLogin() {
  await loginFormRef.value?.validate()
  loading.value = true
  try {
    const { data } = await authApi.login(loginForm.username, loginForm.password)
    auth.setAuth(data.token, data.user_id, { username: data.username, role: data.role })
    ElMessage.success(`欢迎回来，${data.username}`)
    router.push('/documents')
  } catch (e) {
    ElMessage.error(e.response?.data?.error || '登录失败')
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  await registerFormRef.value?.validate()
  loading.value = true
  try {
    await authApi.register(registerForm.username, registerForm.password)
    ElMessage.success('注册成功，请登录')
    activeTab.value = 'login'
    loginForm.username = registerForm.username
  } catch (e) {
    ElMessage.error(e.response?.data?.error || '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  background: var(--brand-ink);
}
.login-brand {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 48px 64px;
  color: #fff;
}
.brand-top { display: flex; align-items: center; gap: 10px; }
.brand-mark { color: #4fd1c5; }
.brand-name { font-size: 18px; font-weight: 600; letter-spacing: 1px; }
.brand-mid h1 {
  font-size: 30px;
  line-height: 1.35;
  margin: 0 0 14px;
  font-weight: 600;
  max-width: 460px;
}
.brand-mid p {
  color: var(--brand-ink-text);
  font-size: 14px;
  line-height: 1.8;
  max-width: 440px;
  margin: 0 0 28px;
}
.brand-points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.brand-points li {
  position: relative;
  padding-left: 20px;
  color: var(--brand-ink-text);
  font-size: 13px;
}
.brand-points li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #4fd1c5;
}
.brand-foot {
  color: #56617c;
  font-size: 12px;
}
.login-panel {
  width: 400px;
  background: #fff;
  padding: 48px 44px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
@media (max-width: 900px) {
  .login-brand { display: none; }
  .login-panel { width: 100%; }
}
</style>
