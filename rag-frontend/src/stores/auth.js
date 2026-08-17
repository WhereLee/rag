import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const token = ref('')
  const user = ref(null)  // { username, role }

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

  function setAuth(tokenVal, userVal) {
    token.value = tokenVal
    user.value = userVal
    localStorage.setItem('rag_token', tokenVal)
    localStorage.setItem('rag_user', JSON.stringify(userVal))
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('rag_token')
    localStorage.removeItem('rag_user')
  }

  function restoreSession() {
    const savedToken = localStorage.getItem('rag_token')
    const savedUser = localStorage.getItem('rag_user')
    if (savedToken && savedUser) {
      token.value = savedToken
      try {
        user.value = JSON.parse(savedUser)
      } catch (e) {
        logout()
      }
    }
  }

  return { token, user, isLoggedIn, isAdmin, setAuth, logout, restoreSession }
})
