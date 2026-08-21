import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const token = ref('')
  const userId = ref(null)
  const user = ref(null)  // { username, role }

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

  function setAuth(tokenVal, userIdVal, userVal) {
    token.value = tokenVal
    userId.value = userIdVal
    user.value = userVal
    localStorage.setItem('rag_token', tokenVal)
    localStorage.setItem('rag_user_id', userIdVal)
    localStorage.setItem('rag_user', JSON.stringify(userVal))
  }

  function logout() {
    token.value = ''
    userId.value = null
    user.value = null
    localStorage.removeItem('rag_token')
    localStorage.removeItem('rag_user_id')
    localStorage.removeItem('rag_user')
  }

  function restoreSession() {
    const savedToken = localStorage.getItem('rag_token')
    const savedUserId = localStorage.getItem('rag_user_id')
    const savedUser = localStorage.getItem('rag_user')
    if (savedToken && savedUserId && savedUser) {
      token.value = savedToken
      userId.value = parseInt(savedUserId)
      try {
        user.value = JSON.parse(savedUser)
      } catch (e) {
        logout()
      }
    }
  }

  return { token, userId, user, isLoggedIn, isAdmin, setAuth, logout, restoreSession }
})
