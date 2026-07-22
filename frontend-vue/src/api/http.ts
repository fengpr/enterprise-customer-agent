import axios from 'axios'
import { ElMessage } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

let handlingUnauthorized = false

export const agentHttp = axios.create({
  baseURL: '/api',
  timeout: 90000
})

export const businessHttp = axios.create({
  baseURL: '/business-api',
  timeout: 10000
})

for (const client of [agentHttp, businessHttp]) {
  client.interceptors.request.use((config) => {
    const auth = useAuthStore()
    if (auth.token) {
      config.headers.Authorization = `Bearer ${auth.token}`
    }
    return config
  })

  client.interceptors.response.use(
    (response) => response,
    (error) => {
      const requestUrl = String(error.config?.url || '')
      if (error.response?.status === 401 && !requestUrl.includes('/auth/login')) {
        const auth = useAuthStore()
        const role = auth.user?.role
        const loginPath = role === 'staff' ? '/staff/login' : '/customer/login'

        // 登录态失效时清理本地缓存，避免页面继续用旧 token 批量请求受保护接口。
        auth.logout()
        if (!handlingUnauthorized && window.location.pathname !== loginPath) {
          handlingUnauthorized = true
          ElMessage.error('登录已失效，请重新登录')
          window.location.assign(loginPath)
        }
        return Promise.reject(error)
      }

      const detail = error.response?.data?.detail ?? error.response?.data?.error_message
      const message = typeof detail === 'string' ? detail : error.message
      ElMessage.error(message || '请求失败')
      return Promise.reject(error)
    }
  )
}
