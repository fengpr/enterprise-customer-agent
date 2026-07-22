import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import CustomerHome from '@/views/customer/CustomerHome.vue'
import CustomerDashboard from '@/views/customer/CustomerDashboard.vue'
import CustomerOrders from '@/views/customer/CustomerOrders.vue'
import CustomerTickets from '@/views/customer/CustomerTickets.vue'
import CustomerHelp from '@/views/customer/CustomerHelp.vue'
import CustomerNotifications from '@/views/customer/CustomerNotifications.vue'
import CustomerProfile from '@/views/customer/CustomerProfile.vue'
import CustomerLogin from '@/views/customer/CustomerLogin.vue'
import StaffHome from '@/views/staff/StaffHome.vue'
import StaffLogin from '@/views/staff/StaffLogin.vue'
import RagEvaluation from '@/views/staff/RagEvaluation.vue'
import SystemMonitor from '@/views/staff/SystemMonitor.vue'

type UserRole = 'customer' | 'staff'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/customer' },
    { path: '/customer/login', component: CustomerLogin, meta: { guest: true } },
    { path: '/customer', component: CustomerDashboard, meta: { role: 'customer' } },
    { path: '/customer/orders', component: CustomerOrders, meta: { role: 'customer' } },
    { path: '/customer/tickets', component: CustomerTickets, meta: { role: 'customer' } },
    { path: '/customer/help', component: CustomerHelp, meta: { role: 'customer' } },
    { path: '/customer/notifications', component: CustomerNotifications, meta: { role: 'customer' } },
    { path: '/customer/profile', component: CustomerProfile, meta: { role: 'customer' } },
    // 在线客服保留原有工作台，首页仅负责提供客户自助入口和信息概览。
    { path: '/customer/service', component: CustomerHome, meta: { role: 'customer' } },
    { path: '/staff/login', component: StaffLogin, meta: { guest: true } },
    { path: '/staff', component: StaffHome, meta: { role: 'staff' } },
    { path: '/staff/rag-evaluation', component: RagEvaluation, meta: { role: 'staff' } },
    { path: '/staff/system-monitor', component: SystemMonitor, meta: { role: 'staff' } }
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
  return '/customer/login'
}

function homePath(role: UserRole) {
  if (role === 'staff') return '/staff'
  return '/customer'
}

export default router
