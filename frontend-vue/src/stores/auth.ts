import { defineStore } from 'pinia'

import { authApi } from '@/api/auth'
import { useCustomerSessionStore } from '@/stores/customerSessions'
import type { CurrentUser } from '@/types/api'

const TOKEN_KEY = 'eca_token'
const USER_KEY = 'eca_user'

function readStoredUser(): CurrentUser | null {
  const raw = localStorage.getItem(USER_KEY)
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
    token: localStorage.getItem(TOKEN_KEY),
    user: readStoredUser()
  }),
  actions: {
    async login(username: string, password: string) {
      const { data } = await authApi.login(username, password)
      useCustomerSessionStore().clear()
      this.token = data.token
      this.user = data.user
      localStorage.setItem(TOKEN_KEY, data.token)
      localStorage.setItem(USER_KEY, JSON.stringify(data.user))
      return data.user
    },
    logout() {
      useCustomerSessionStore().clear()
      this.token = null
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
    },
    async refreshCurrentUser() {
      const { data } = await authApi.currentUser()
      this.user = data
      localStorage.setItem(USER_KEY, JSON.stringify(data))
      return data
    }
  }
})
