import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import CustomerHome from '@/views/customer/CustomerHome.vue'
import CustomerLogin from '@/views/customer/CustomerLogin.vue'
import DispatcherHome from '@/views/dispatcher/DispatcherHome.vue'
import DispatcherLogin from '@/views/dispatcher/DispatcherLogin.vue'
import StaffHome from '@/views/staff/StaffHome.vue'
import StaffLogin from '@/views/staff/StaffLogin.vue'
import RagEvaluation from '@/views/staff/RagEvaluation.vue'

type UserRole = 'customer' | 'staff' | 'dispatcher'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/customer' },
    { path: '/customer/login', component: CustomerLogin, meta: { guest: true } },
    { path: '/customer', component: CustomerHome, meta: { role: 'customer' } },
    { path: '/staff/login', component: StaffLogin, meta: { guest: true } },
    { path: '/staff', component: StaffHome, meta: { role: 'staff' } },
    { path: '/staff/rag-evaluation', component: RagEvaluation, meta: { role: 'staff' } },
    { path: '/dispatcher/login', component: DispatcherLogin, meta: { guest: true } },
    { path: '/dispatcher', component: DispatcherHome, meta: { role: 'dispatcher' } }
  ]
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  const requiredRole = to.meta.role as UserRole | undefined

  if (!requiredRole) {
    return true
  }

  if (!auth.token || !auth.user) {
    return loginPath(requiredRole)
  }

  if (auth.user.role !== requiredRole) {
    return homePath(auth.user.role as UserRole)
  }

  return true
})

function loginPath(role: UserRole) {
  if (role === 'staff') return '/staff/login'
  if (role === 'dispatcher') return '/dispatcher/login'
  return '/customer/login'
}

function homePath(role: UserRole) {
  if (role === 'staff') return '/staff'
  if (role === 'dispatcher') return '/dispatcher'
  return '/customer'
}

export default router
