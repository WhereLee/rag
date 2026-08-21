<template>
  <el-container class="app-layout">
    <el-aside width="200px" class="sidebar">
      <div class="logo">
        <el-icon :size="22" class="logo-icon"><Document /></el-icon>
        <span>文档问答</span>
      </div>
      <el-menu :default-active="currentRoute" router class="sidebar-menu">
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon>
          <span>智能问答</span>
        </el-menu-item>
        <el-menu-item index="/documents">
          <el-icon><Document /></el-icon>
          <span>文档管理</span>
        </el-menu-item>
        <el-menu-item v-if="auth.isAdmin" index="/admin">
          <el-icon><Setting /></el-icon>
          <span>管理面板</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-footer">
        <div class="user-info">
          <el-icon><Avatar /></el-icon>
          <span>{{ auth.user?.username }}</span>
          <el-tag size="small" :type="auth.isAdmin ? 'danger' : 'info'" style="margin-left:4px">
            {{ auth.user?.role }}
          </el-tag>
        </div>
        <el-button text type="danger" @click="handleLogout">
          <el-icon><SwitchButton /></el-icon>
          退出
        </el-button>
      </div>
    </el-aside>
    <el-main class="main-content">
      <slot />
    </el-main>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const currentRoute = computed(() => route.path)

function handleLogout() {
  auth.logout()
  ElMessage.success('已退出')
  router.push('/login')
}
</script>

<style scoped>
.app-layout { height: 100vh; }
.sidebar {
  background: var(--brand-ink);
  display: flex;
  flex-direction: column;
}
.logo {
  padding: 18px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: #fff;
  border-bottom: 1px solid rgba(255,255,255,0.08);
}
.logo-icon { color: #4fd1c5; }
.sidebar-menu { flex: 1; border-right: none; background: transparent; }
.sidebar-menu :deep(.el-menu-item) { color: var(--brand-ink-text); }
.sidebar-menu :deep(.el-menu-item:hover) {
  background: var(--brand-ink-soft);
  color: var(--brand-ink-text-hi);
}
.sidebar-menu :deep(.el-menu-item.is-active) {
  background: var(--brand-ink-soft);
  color: #fff;
  border-right: 2px solid #4fd1c5;
}
.sidebar-footer {
  padding: 12px 16px;
  border-top: 1px solid rgba(255,255,255,0.08);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.user-info {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--brand-ink-text);
}
.user-info :deep(.el-tag) { border-color: transparent; }
.main-content {
  background: var(--brand-bg);
  padding: 20px;
  overflow-y: auto;
}
</style>
