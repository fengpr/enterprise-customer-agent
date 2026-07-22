<script setup lang="ts">
defineOptions({ name: 'CustomerProfile' })

import { ChatDotRound, Document, Refresh, Tickets, UserFilled, Van } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { storeToRefs } from 'pinia'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { customerApi } from '@/api/customer'
import CustomerSidebar from '@/components/customer/CustomerSidebar.vue'
import { useAuthStore } from '@/stores/auth'
import { useCustomerSessionStore } from '@/stores/customerSessions'
import type { CustomerOrder, Ticket } from '@/types/api'

const router = useRouter()
const auth = useAuthStore()
const sessionStore = useCustomerSessionStore()
const { sessions } = storeToRefs(sessionStore)
const orders = ref<CustomerOrder[]>([])
const tickets = ref<Ticket[]>([])
const loading = ref(false)

/** 根据当前客户实际业务数据生成个人服务概览，不展示内部客户 ID 等敏感字段。 */
const overview = computed(() => ({
  orderCount: orders.value.length,
  processingTicketCount: tickets.value.filter((ticket) => ['PENDING_ASSIGN', 'PENDING_PROCESS', 'PROCESSING', 'REOPENED', 'TRANSFERRED'].includes(String(ticket.status))).length,
  sessionCount: sessions.value.length,
  signedOrderCount: orders.value.filter((order) => order.orderStatus === 'SIGNED').length
}))

const accountRows = computed(() => [
  { label: '账户名称', value: auth.user?.display_name || '—' },
  { label: '账户类型', value: '客户账户' },
  { label: '登录状态', value: auth.token ? '当前已登录' : '未登录' },
  { label: '账户服务', value: '订单查询、工单跟进、智能客服' }
])

function openService() { void router.push('/customer/service?new=1') }
function openSession(sessionId: string) { void router.push({ path: '/customer/service', query: { sessionId } }) }
function unavailable() { ElMessage.info('该功能正在建设中') }

/** 账号资料与服务数据分别回源，单项失败不影响其余信息展示。 */
async function loadProfile(showFeedback = false) {
  if (loading.value) return
  loading.value = true
  try {
    const [profileResult, orderResult, ticketResult, sessionResult] = await Promise.allSettled([
      auth.refreshCurrentUser(),
      customerApi.orders(),
      customerApi.tickets(),
      sessionStore.refresh(true)
    ])
    if (orderResult.status === 'fulfilled') orders.value = orderResult.value.data
    if (ticketResult.status === 'fulfilled') tickets.value = ticketResult.value.data
    if (showFeedback && [profileResult, orderResult, ticketResult, sessionResult].some((result) => result.status === 'fulfilled')) {
      ElMessage.success('个人信息已刷新')
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => { void loadProfile() })
onActivated(() => { void loadProfile() })
</script>

<template>
  <div class="customer-profile-page">
    <CustomerSidebar :user="auth.user" :sessions="sessions" @contact-service="openService" @unavailable="unavailable" @select-session="openSession" @sessions-changed="() => sessionStore.refresh(true)" />

    <main class="customer-profile-main">
      <header class="customer-page-header profile-header">
        <div>
          <p class="profile-eyebrow">ACCOUNT CENTER</p>
          <h1>个人中心</h1>
          <p>查看当前登录账户和您的服务使用概览。</p>
        </div>
        <el-button :icon="Refresh" :loading="loading" @click="loadProfile(true)">刷新信息</el-button>
      </header>

      <section class="profile-hero-card">
        <div class="profile-avatar"><UserFilled /></div>
        <div class="profile-identity"><span>当前登录账户</span><h2>{{ auth.user?.display_name || '客户' }}</h2><p>您的订单、会话与工单仅在当前登录账户下可见。</p></div>
        <div class="profile-status"><i></i><span>账户状态正常</span></div>
      </section>

      <section class="profile-metrics">
        <article><i class="blue"><Document /></i><span>全部订单</span><b>{{ overview.orderCount }}</b><small>当前账户订单总数</small></article>
        <article><i class="green"><Tickets /></i><span>处理中工单</span><b>{{ overview.processingTicketCount }}</b><small>等待或正在处理</small></article>
        <article><i class="purple"><ChatDotRound /></i><span>历史会话</span><b>{{ overview.sessionCount }}</b><small>当前账户客服会话</small></article>
        <article><i class="orange"><Van /></i><span>已完成订单</span><b>{{ overview.signedOrderCount }}</b><small>已签收订单数量</small></article>
      </section>

      <section class="profile-content-grid">
        <article class="profile-card">
          <div class="profile-card-title"><h2>账户信息</h2><span>由登录身份服务提供</span></div>
          <dl class="profile-account-list"><template v-for="row in accountRows" :key="row.label"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></template></dl>
        </article>
        <article class="profile-card profile-support-card">
          <div class="profile-card-title"><h2>服务与隐私</h2></div>
          <p>账号身份以当前登录状态为准。订单、工单和会话数据均按账户隔离展示。</p>
          <p>如需处理账户资料、订单归属等问题，请通过客服发起人工核验。</p>
          <el-button type="primary" plain @click="openService">联系在线客服</el-button>
        </article>
      </section>
    </main>
  </div>
</template>

<style scoped>
.customer-profile-page { min-height: 100vh; display: grid; grid-template-columns: 280px minmax(0, 1fr); background: #f4f7fb; }
.customer-profile-main { min-width: 0; padding: 30px clamp(22px, 4vw, 64px); }
.profile-header { margin-bottom: 22px; }
.profile-header h1 { margin: 4px 0 8px; color: #15233b; font-size: 28px; }
.profile-header p { margin: 0; color: #73829a; }
.profile-eyebrow { color: #2878f0 !important; font-size: 12px; font-weight: 800; letter-spacing: .12em; }
.profile-hero-card { display: flex; align-items: center; gap: 18px; padding: 28px; border: 1px solid #dceaff; border-radius: 20px; background: linear-gradient(120deg, #fff 0%, #f3f8ff 100%); box-shadow: 0 14px 32px rgba(40, 82, 142, .08); }
.profile-avatar { display: grid; flex: 0 0 auto; place-items: center; width: 68px; height: 68px; border-radius: 22px; color: #fff; background: linear-gradient(135deg, #1d71ed, #5da8ff); font-size: 34px; }
.profile-identity span { color: #7a8ba4; font-size: 13px; }.profile-identity h2 { margin: 5px 0; color: #172b4d; font-size: 22px; }.profile-identity p { margin: 0; color: #60728c; }
.profile-status { display: flex; align-items: center; gap: 7px; margin-left: auto; color: #12864e; font-weight: 700; }.profile-status i { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 0 5px rgba(34, 197, 94, .13); }
.profile-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin: 20px 0; }.profile-metrics article { display: grid; grid-template-columns: 42px 1fr; gap: 3px 12px; align-items: center; padding: 18px; border: 1px solid #e2eaf5; border-radius: 15px; background: #fff; }.profile-metrics i { display: grid; grid-row: 1 / 4; place-items: center; width: 42px; height: 42px; border-radius: 12px; font-size: 20px; }.profile-metrics i.blue { color: #1976ed; background: #eaf3ff; }.profile-metrics i.green { color: #15935e; background: #eafaf3; }.profile-metrics i.purple { color: #7c4dff; background: #f2edff; }.profile-metrics i.orange { color: #e07a18; background: #fff3e6; }.profile-metrics span { color: #71819a; font-size: 13px; }.profile-metrics b { color: #172b4d; font-size: 23px; }.profile-metrics small { color: #95a2b5; font-size: 12px; }
.profile-content-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(300px, .75fr); gap: 20px; }.profile-card { padding: 24px; border: 1px solid #e2eaf5; border-radius: 17px; background: #fff; }.profile-card-title { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }.profile-card-title h2 { margin: 0; color: #172b4d; font-size: 18px; }.profile-card-title span { color: #8a98aa; font-size: 12px; }.profile-account-list { display: grid; grid-template-columns: 128px 1fr; margin: 0; }.profile-account-list dt, .profile-account-list dd { margin: 0; padding: 14px 0; border-top: 1px solid #edf1f6; }.profile-account-list dt { color: #7b8aa1; }.profile-account-list dd { color: #2a3b55; font-weight: 600; }.profile-support-card p { color: #667892; line-height: 1.8; }
@media (max-width: 1080px) { .profile-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }.profile-content-grid { grid-template-columns: 1fr; } }
@media (max-width: 900px) { .customer-profile-page { display: block; }.customer-profile-main { padding: 22px 18px; }.profile-hero-card { align-items: flex-start; flex-wrap: wrap; }.profile-status { width: 100%; margin-left: 86px; } }
</style>
