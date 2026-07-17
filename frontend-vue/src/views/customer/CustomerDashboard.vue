<script setup lang="ts">
defineOptions({ name: 'CustomerDashboard' })

import { Bell, ChatDotRound, Document, Headset, HelpFilled, House, Refresh, Tickets, Van } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { storeToRefs } from 'pinia'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import CustomerSidebar from '@/components/customer/CustomerSidebar.vue'
import { customerApi } from '@/api/customer'
import { useAuthStore } from '@/stores/auth'
import { useCustomerSessionStore } from '@/stores/customerSessions'
import type { CustomerOrder, Ticket } from '@/types/api'

const router = useRouter()
const auth = useAuthStore()
const sessionStore = useCustomerSessionStore()
const { sessions } = storeToRefs(sessionStore)
const refreshing = ref(false)
const orders = ref<CustomerOrder[]>([])
const tickets = ref<Ticket[]>([])
const loaded = ref(false)
const user = computed(() => ({
  name: auth.user?.display_name || '客户',
  level: '普通客户',
  // 当前客户资料接口没有积分字段，首页明确显示暂无数据，避免继续展示原型中的虚构积分。
  points: '—',
  totalOrders: orders.value.length,
  totalTickets: tickets.value.length
}))

/** 将业务状态转为客户可理解的订单状态，不依赖原型固定文案。 */
function orderStatus(order: CustomerOrder) {
  if (order.afterSaleStatus && order.afterSaleStatus !== 'NONE') return { label: '售后中', tone: '售后中' }
  if (order.orderStatus === 'SIGNED') return { label: '已完成', tone: '已完成' }
  if (order.orderStatus === 'PENDING_PAYMENT') return { label: '待付款', tone: '待付款' }
  if (['SHIPPED', 'IN_TRANSIT', 'OUT_FOR_DELIVERY'].includes(order.orderStatus)) return { label: '运输中', tone: '运输中' }
  return { label: order.orderStatus || '处理中', tone: '运输中' }
}
function ticketStatus(ticket: Ticket) {
  if (ticket.status === 'PENDING_ASSIGN') return { label: '待分派', tone: '待分派' }
  if (['WAITING_SUPPLEMENT', 'PENDING_SUPPLEMENT'].includes(String(ticket.status))) return { label: '待补充', tone: '待补充' }
  if (ticket.status === 'CLOSED') return { label: '已完成', tone: '已完成' }
  if (['CANCELLED', 'REJECTED'].includes(String(ticket.status))) return { label: '已关闭', tone: '已关闭' }
  return { label: '处理中', tone: '处理中' }
}
function ticketType(ticket: Ticket) {
  const labels: Record<string, string> = { RETURN: '退货申请', REFUND: '退款申请', COMPLAINT: '投诉反馈', INVOICE: '发票问题', LOGISTICS: '物流异常', TECH_SUPPORT: '技术支持', SERVICE: '服务工单', GENERAL: '综合咨询' }
  return labels[String(ticket.ticketType || '').toUpperCase()] || ticket.ticketType || '服务工单'
}
function formatTime(value?: string | null) { return value ? value.replace('T', ' ').slice(0, 16) : '—' }
function formatMoney(value?: number | string | null) { return `¥${Number(value || 0).toFixed(0)}` }
function withinDays(value?: string | null, days = 7) {
  if (!value) return false
  const time = new Date(value).getTime()
  return Number.isFinite(time) && Date.now() - time <= days * 24 * 60 * 60 * 1000 && Date.now() >= time
}

const entries = computed(() => [
  { key: 'orders', title: '我的订单', description: '查看订单状态与物流进度', action: '查看全部订单', count: orders.value.length, tone: 'blue' },
  { key: 'tickets', title: '我的工单', description: '跟踪工单处理进度', action: '查看全部工单', count: tickets.value.length, tone: 'green' },
  { key: 'service', title: '智能客服', description: '智能助手与人工客服', action: '立即发起咨询', tone: 'purple' },
  { key: 'help', title: '帮助中心', description: '常见问题与操作指南', action: '进入帮助中心', tone: 'orange' }
])
const metrics = computed(() => {
  const pending = tickets.value.filter((item) => item.status === 'PENDING_ASSIGN').length
  const processing = tickets.value.filter((item) => ['PENDING_PROCESS', 'PROCESSING', 'TRANSFERRED', 'REOPENED'].includes(String(item.status))).length
  const recentOrders = orders.value.filter((item) => withinDays(item.payTime)).length
  const recentSessions = sessions.value.filter((item) => withinDays(item.updated_at)).length
  return [
    { label: '待分派工单', value: pending, note: '等待客服受理', tone: 'blue' },
    { label: '处理中', value: processing, note: '客服正在处理', tone: 'orange' },
    { label: '最近订单', value: recentOrders, note: '近 7 天新增', tone: 'green' },
    { label: '最近会话', value: recentSessions, note: '近 7 天更新', tone: 'purple' }
  ]
})
const recentOrders = computed(() => [...orders.value].sort((a, b) => String(b.payTime || '').localeCompare(String(a.payTime || ''))).slice(0, 3))
const recentTickets = computed(() => [...tickets.value].sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || ''))).slice(0, 5))
const reminders = computed(() => [
  { title: '待分派工单', desc: pendingTicketText.value, action: '去查看', tone: 'orange', key: 'tickets' },
  { title: '处理中工单', desc: processingTicketText.value, action: '去跟进', tone: 'blue', key: 'tickets' }
])
const pendingTicketText = computed(() => `您有 ${tickets.value.filter((item) => item.status === 'PENDING_ASSIGN').length} 个工单等待受理`)
const processingTicketText = computed(() => `您有 ${tickets.value.filter((item) => ['PENDING_PROCESS', 'PROCESSING', 'TRANSFERRED', 'REOPENED'].includes(String(item.status))).length} 个工单处理中`)
const todos = computed(() => [
  { title: '补充申请材料', count: tickets.value.filter((item) => ['WAITING_SUPPLEMENT', 'PENDING_SUPPLEMENT'].includes(String(item.status))).length, key: 'tickets' },
  { title: '查看待分派工单', count: tickets.value.filter((item) => item.status === 'PENDING_ASSIGN').length, key: 'tickets' },
  { title: '跟进处理进度', count: tickets.value.filter((item) => ['PENDING_PROCESS', 'PROCESSING', 'TRANSFERRED', 'REOPENED'].includes(String(item.status))).length, key: 'tickets' },
  { title: '继续咨询客服', count: sessions.value.length, key: 'service' }
])
const activities = computed(() => recentTickets.value.slice(0, 3).map((ticket, index) => ({ time: formatTime(ticket.updatedAt), text: `工单 ${ticket.ticketNo} 当前为【${ticketStatus(ticket).label}】`, tone: index ? 'blue' : 'green', ticketNo: ticket.ticketNo })))

/** 原型页保留真实客服入口，其余未开发功能仅给出明确反馈。 */
function handleEntry(key: string) {
  if (key === 'orders') { void router.push('/customer/orders'); return }
  if (key === 'tickets') { void router.push('/customer/tickets'); return }
  if (key === 'service') { void router.push('/customer/service?new=1'); return }
  if (key === 'help') { void router.push('/customer/service?message=我需要帮助，请给我常用操作指引'); return }
  ElMessage.info('该功能页面暂未开放')
}
function contactService() { void router.push('/customer/service?new=1') }
function openSession(sessionId: string) { void router.push({ path: '/customer/service', query: { sessionId } }) }
function createTicket() { void router.push({ path: '/customer/service', query: { new: '1', message: '我想创建一个新的服务工单，请引导我补充必要信息。' } }) }
function openOrder(order?: CustomerOrder) { void router.push({ path: '/customer/orders', query: order ? { orderNo: order.orderNo } : {} }) }
function openTicket(ticket?: Ticket) { void router.push({ path: '/customer/tickets', query: ticket ? { ticketNo: ticket.ticketNo } : {} }) }
function openService(message?: string, order?: CustomerOrder) { void router.push({ path: '/customer/service', query: { new: '1', ...(message ? { message } : {}), ...(order ? { orderNo: order.orderNo } : {}) } }) }
async function loadHomeData(showFeedback = false) {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const [orderResult, ticketResult, sessionResult] = await Promise.allSettled([customerApi.orders(), customerApi.tickets(), sessionStore.refresh(true)])
    let hasSuccess = false
    // 三类数据独立保留，单个接口短暂失败不应让首页其它真实信息消失。
    if (orderResult.status === 'fulfilled') { orders.value = orderResult.value.data; hasSuccess = true }
    if (ticketResult.status === 'fulfilled') { tickets.value = ticketResult.value.data; hasSuccess = true }
    if (sessionResult.status === 'fulfilled') hasSuccess = true
    if (hasSuccess) {
      loaded.value = true
      if (showFeedback) ElMessage.success('首页数据已刷新')
    } else if (!loaded.value) {
      ElMessage.warning('首页数据暂不可用，请稍后刷新')
    }
  } catch {
    // 兜底保留上一次成功数据，避免首页切换或网络抖动导致内容闪白。
    if (!loaded.value) ElMessage.warning('首页数据暂不可用，请稍后刷新')
  } finally {
    refreshing.value = false
  }
}
async function refresh() {
  await loadHomeData(true)
}
function unavailable() { ElMessage.info('该功能正在建设中') }
const entryIcons = { orders: Document, tickets: Tickets, service: Headset, help: HelpFilled }
const metricIcons = [Document, Tickets, Van, Bell]

onMounted(() => { void loadHomeData() })
onActivated(() => { if (!loaded.value) void loadHomeData() })
</script>

<template>
  <div class="prototype-home">
    <CustomerSidebar :prototype-user="user" :sessions="sessions" @contact-service="contactService" @unavailable="unavailable" @select-session="openSession" @sessions-changed="() => sessionStore.refresh(true)" />
    <main class="prototype-main">
      <header class="prototype-header customer-page-header">
        <div><h1>客户自助服务首页</h1><p>欢迎回来，您可以在这里快速查看订单、工单、服务进度与常用入口</p></div>
        <div class="prototype-header-actions customer-page-header-actions"><el-button :icon="Refresh" :loading="refreshing" @click="refresh">刷新数据</el-button><el-button :icon="Document" @click="createTicket">新建工单</el-button><el-button type="primary" :icon="ChatDotRound" @click="contactService">新建咨询</el-button></div>
      </header>
      <div class="prototype-grid">
        <section class="prototype-center">
          <section class="welcome-prototype-card"><div class="welcome-copy"><h2>您好，{{ user.name }} 👋</h2><p>感谢您信任我们的服务，智能助手 7×24 小时为您提供支持。</p><p>如需帮助，您可以随时发起咨询或提交工单。</p><div class="welcome-summary"><span><i>▣</i><small>会员等级</small><b>{{ user.level }}</b></span><span><i>◆</i><small>账户积分</small><b>{{ user.points }}</b></span><span><i>▤</i><small>累计订单</small><b>{{ user.totalOrders }}</b></span><span><i>▤</i><small>累计工单</small><b>{{ user.totalTickets }}</b></span></div></div><div class="welcome-illustration" aria-hidden="true"><div class="illus-panel"><b></b><b></b><b></b></div><div class="illus-phone">⌁</div><div class="illus-chart">▮▮▮</div></div></section>
          <section class="prototype-entry-grid"><button v-for="entry in entries" :key="entry.key" class="prototype-entry" :class="entry.tone" @click="handleEntry(entry.key)"><el-icon><component :is="entryIcons[entry.key as keyof typeof entryIcons]" /></el-icon><span><b>{{ entry.title }}</b><small>{{ entry.description }}</small><em>{{ entry.action }}<template v-if="typeof entry.count === 'number'"> {{ entry.count }}</template> ›</em></span></button></section>
          <section class="prototype-metric-grid"><article v-for="(metric, index) in metrics" :key="metric.label" class="prototype-metric"><el-icon :class="metric.tone"><component :is="metricIcons[index]" /></el-icon><span>{{ metric.label }}</span><b>{{ metric.value }}</b><small>{{ metric.note }}</small></article></section>
          <section class="prototype-list-grid"><article class="prototype-panel"><div class="prototype-panel-head"><h2>最近订单</h2><button @click="openOrder()">查看全部 ›</button></div><button v-for="order in recentOrders" :key="order.orderNo" class="prototype-order" @click="openOrder(order)"><i :class="orderStatus(order).tone">▰</i><span><b>{{ order.productName || '商品信息暂不可用' }}</b><small>订单号：{{ order.orderNo }}</small></span><span><b>{{ formatMoney(order.amount) }}</b><small>下单时间：{{ formatTime(order.payTime) }}</small></span><em :class="orderStatus(order).tone">{{ orderStatus(order).label }}</em></button><el-empty v-if="!recentOrders.length && loaded" description="暂无订单" :image-size="48" /></article><article class="prototype-panel"><div class="prototype-panel-head"><h2>我的工单进展</h2><button @click="openTicket()">查看全部 ›</button></div><div class="prototype-ticket-head"><span>工单号</span><span>类型</span><span>当前状态</span><span>更新时间</span></div><button v-for="ticket in recentTickets" :key="ticket.ticketNo" class="prototype-ticket" @click="openTicket(ticket)"><b :title="ticket.ticketNo">{{ ticket.ticketNo }}</b><span>{{ ticketType(ticket) }}</span><em :class="ticketStatus(ticket).tone">{{ ticketStatus(ticket).label }}</em><time>{{ formatTime(ticket.updatedAt) }}</time></button><el-empty v-if="!recentTickets.length && loaded" description="暂无工单" :image-size="48" /></article></section>
          <section class="prototype-services orders-quick-services"><h2>快捷服务</h2><button @click="openOrder(recentOrders[0])"><Van /><span>查询物流</span><small>查看物流跟踪进度</small></button><button @click="openService('我想申请退货退款，请帮我确认需要提供的信息。', recentOrders[0])"><i>◉</i><span>申请退货退款</span><small>退货/退款申请入口</small></button><button @click="contactService"><Headset /><span>联系客服</span><small>联系在线智能客服</small></button><button @click="openService('我需要咨询发票问题。', recentOrders[0])"><i>▣</i><span>发票问题</span><small>发票申请与咨询</small></button></section>
        </section>
        <aside class="prototype-right"><section class="prototype-side-card"><h2>服务提醒 <span>−</span></h2><button v-for="reminder in reminders" :key="reminder.title" class="prototype-reminder" @click="handleEntry(reminder.key)"><i :class="reminder.tone"></i><span><b>{{ reminder.title }}</b><small>{{ reminder.desc }}</small></span><em>{{ reminder.action }} ›</em></button></section><section class="prototype-side-card"><h2>待办事项 <span>−</span></h2><button v-for="todo in todos" :key="todo.title" class="prototype-todo" @click="handleEntry(todo.key)"><span>□ {{ todo.title }}</span><b>{{ todo.count }}</b></button><button class="side-all" @click="openTicket()">查看全部待办 ›</button></section><section class="prototype-side-card progress-side"><h2>最新进度 <span>−</span></h2><button v-for="activity in activities" :key="`${activity.ticketNo}-${activity.time}`" class="prototype-activity" @click="openTicket(tickets.find((ticket) => ticket.ticketNo === activity.ticketNo))"><i :class="activity.tone"></i><time>{{ activity.time }}</time><p>{{ activity.text }}</p></button><el-empty v-if="!activities.length && loaded" description="暂无进度" :image-size="48" /><button class="side-all" @click="openTicket()">查看全部进度 ›</button></section></aside>
      </div>
    </main>
  </div>
</template>
