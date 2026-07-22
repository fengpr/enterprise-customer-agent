import { defineStore } from 'pinia'

import { authApi } from '@/api/auth'
import { useCustomerSessionStore } from '@/stores/customerSessions'
import type { CurrentUser } from '@/types/api'

const TOKEN_KEY = 'eca_token'
const USER_KEY = 'eca_user'
const SESSION_TOKEN_KEY = 'eca_session_token'
const SESSION_USER_KEY = 'eca_session_user'

function readStoredUser(): CurrentUser | null {
  const raw = localStorage.getItem(USER_KEY) || sessionStorage.getItem(SESSION_USER_KEY)
  if (!raw) {
    return null
  }
  try {
    return JSON.parse(raw) as CurrentUser
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(SESSION_TOKEN_KEY),
    user: readStoredUser()
  }),
  actions: {
    async login(username: string, password: string, remember = true) {
      const { data } = await authApi.login(username, password)
      useCustomerSessionStore().clear()
      this.token = data.token
      this.user = data.user
      // “记住我”使用持久化存储；未勾选时仅保存到当前浏览器会话，关闭标签页后自动失效。
      const storage = remember ? localStorage : sessionStorage
      const tokenKey = remember ? TOKEN_KEY : SESSION_TOKEN_KEY
      const userKey = remember ? USER_KEY : SESSION_USER_KEY
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
      sessionStorage.removeItem(SESSION_TOKEN_KEY)
      sessionStorage.removeItem(SESSION_USER_KEY)
      storage.setItem(tokenKey, data.token)
      storage.setItem(userKey, JSON.stringify(data.user))
      return data.user
    },
    logout() {
      useCustomerSessionStore().clear()
      this.token = null
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
      sessionStorage.removeItem(SESSION_TOKEN_KEY)
      sessionStorage.removeItem(SESSION_USER_KEY)
    },
    async refreshCurrentUser() {
      const { data } = await authApi.currentUser()
      this.user = data
      const storage = localStorage.getItem(TOKEN_KEY) ? localStorage : sessionStorage
      storage.setItem(storage === localStorage ? USER_KEY : SESSION_USER_KEY, JSON.stringify(data))
      return data
    }
  }
})
