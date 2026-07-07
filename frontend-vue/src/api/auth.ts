import { agentHttp } from './http'
import type { CurrentUser, LoginResponse } from '@/types/api'

export const authApi = {
  login(username: string, password: string) {
    return agentHttp.post<LoginResponse>('/auth/login', { username, password })
  },
  currentUser() {
    return agentHttp.get<CurrentUser>('/auth/current-user')
  }
}
