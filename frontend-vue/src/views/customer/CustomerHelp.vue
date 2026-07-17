<script setup lang="ts">
defineOptions({ name: 'CustomerHelp' })

import { ChatDotRound, CircleCheckFilled, Document, EditPen, Headset, QuestionFilled, Refresh, Search, Service, Tickets, Van, Wallet } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { storeToRefs } from 'pinia'
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { customerApi } from '@/api/customer'
import CustomerSidebar from '@/components/customer/CustomerSidebar.vue'
import { useAuthStore } from '@/stores/auth'
import { useCustomerSessionStore } from '@/stores/customerSessions'
import type { AgentStatus, CustomerOrder, Ticket } from '@/types/api'

type HelpCategory = 'all' | 'order' | 'afterSale' | 'invoice' | 'account'

interface HelpFaq {
  id: string
  category: Exclude<HelpCategory, 'all'>
  question: string
  answer: string
}

const router = useRouter()
const auth = useAuthStore()
const sessionStore = useCustomerSessionStore()
const { sessions } = storeToRefs(sessionStore)
const orders = ref<CustomerOrder[]>([])
const tickets = ref<Ticket[]>([])
const agentStatus = ref<AgentStatus | null>(null)
const refreshing = ref(false)
const loaded = ref(false)
const keyword = ref('')
const activeCategory = ref<HelpCategory>('all')
const openedFaqId = ref('order-logistics')

const faqs: HelpFaq[] = [
  { id: 'order-logistics', category: 'order', question: '如何查询订单物流进度？', answer: '进入“我的订单”，选择对应订单后点击“查看物流”，即可查看承运商、运单号和已同步的物流轨迹。' },
  { id: 'after-sale-material', category: 'afterSale', question: '申请退货退款需要准备什么材料？', answer: '请准备订单号、商品问题说明和必要的图片或凭证。智能客服会根据订单状态逐步收集信息，并按现有售后流程提交工单。' },
  { id: 'invoice-apply', category: 'invoice', question: '发票问题如何咨询？', answer: '可从页面底部“发票问题”进入智能客服，说明关联订单、开票类型和具体诉求，客服会引导您补充必要信息。' },
  { id: 'ticket-progress', category: 'afterSale', question: '工单处理进度在哪里查看？', answer: '进入“我的工单”即可查看客户可见状态、更新时间和处理进度。未关闭的工单还可在页面中发起催办。' },
  { id: 'contact-human', category: 'account', question: '如何联系人工客服？', answer: '进入智能客服后点击“转人工客服”。人工服务请求会进入现有排队与接入流程，您可以继续补充问题上下文。' },
  { id: 'account-sessions', category: 'account', question: '如何查看或管理历史会话？', answer: '左侧“最近会话”展示当前账号的真实会话。点击“查看全部”可展开列表，右键会话可执行置顶、取消置顶或删除。' }
]

const categories = [
  { key: 'order' as const, title: '订单帮助', description: '订单状态、支付与物流问题', action: '查看专题', tone: 'blue', icon: Tickets },
  { key: 'afterSale' as const, title: '售后与退款', description: '退货、退款、换货与售后说明', action: '查看专题', tone: 'orange', icon: Wallet },
  { key: 'invoice' as const, title: '账户与发票', description: '账户信息与发票问题', action: '查看专题', tone: 'purple', icon: Document },
  { key: 'account' as const, title: '在线客服', description: '智能助手与人工客服支持', action: '立即咨询', tone: 'green', icon: ChatDotRound }
]

const guides = [
  { title: '订单查询指南', description: '查询订单状态并查看物流进度', tone: 'blue', icon: Tickets, action: 'orders' },
  { title: '退货退款操作流程', description: '通过智能客服提交售后诉求', tone: 'orange', icon: Wallet, action: 'refund' },
  { title: '发票申请说明', description: '咨询开票条件与必要信息', tone: 'purple', icon: Document, action: 'invoice' },
  { title: '联系客服与提交工单', description: '咨询问题或创建服务工单', tone: 'green', icon: Headset, action: 'service' }
]

const categoryLabels: Record<HelpFaq['category'], string> = { order: '订单', afterSale: '售后', invoice: '发票', account: '账户' }
const currentUser = computed(() => ({ name: auth.user?.display_name || '客户', level: '普通客户', points: '—' }))
const agentOnline = computed(() => ['ok', 'healthy', 'success'].includes(String(agentStatus.value?.status || '').toLowerCase()))
const afterSaleOrders = computed(() => orders.value.filter((item) => item.afterSaleStatus && item.afterSaleStatus !== 'NONE').length)
const pendingTickets = computed(() => tickets.value.filter((item) => item.status !== 'CLOSED').length)
const filteredFaqs = computed(() => {
  const query = keyword.value.trim().toLowerCase()
  return faqs.filter((faq) => {
    const matchesCategory = activeCategory.value === 'all' || faq.category === activeCategory.value
    const matchesKeyword = !query || `${faq.question} ${faq.answer}`.toLowerCase().includes(query)
    return matchesCategory && matchesKeyword
  })
})
const helpMetrics = computed(() => [
  { label: '帮助内容', value: faqs.length + guides.length, note: '当前可用内容', tone: 'blue', icon: Document },
  { label: '热门问题', value: faqs.length, note: '常见问题解答', tone: 'orange', icon: QuestionFilled },
  { label: '待处理工单', value: pendingTickets.value, note: '当前账户真实工单', tone: 'purple', icon: Tickets },
  { label: '智能客服', value: agentOnline.value ? '在线' : '暂不可用', note: '7×24 小时服务入口', tone: 'green', icon: Headset }
])
const recentEvents = computed(() => [...tickets.value]
  .sort((left, right) => String(right.updatedAt || '').localeCompare(String(left.updatedAt || '')))
  .slice(0, 3)
  .map((ticket) => ({ ticketNo: ticket.ticketNo, time: formatTime(ticket.updatedAt), text: `工单 ${ticket.ticketNo} 当前状态为【${ticketStatus(ticket.status)}】` })))

function formatTime(value?: string | null) { return value ? value.replace('T', ' ').slice(5, 16) : '—' }
function ticketStatus(status?: string) {
  if (status === 'PENDING_ASSIGN') return '待分派'
  if (['PENDING_PROCESS', 'PROCESSING', 'TRANSFERRED', 'REOPENED'].includes(String(status))) return '处理中'
  if (['PENDING_SUPPLEMENT', 'WAITING_SUPPLEMENT'].includes(String(status))) return '待补充'
  if (status === 'CLOSED') return '已完成'
  return status || '待处理'
}
/** 普通客服入口复用最近会话；只有明确点击“新建咨询”时才允许创建会话。 */
function openService(message?: string, createNew = false) {
  const latestSession = [...sessions.value].sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0]
  const query: Record<string, string> = message ? { message } : {}
  if (createNew) query.new = '1'
  else if (latestSession) query.sessionId = latestSession.session_id
  void router.push({ path: '/customer/service', query })
}
function openSession(sessionId: string) { void router.push({ path: '/customer/service', query: { sessionId } }) }
function chooseCategory(category: HelpCategory) {
  if (category === 'account') {
    activeCategory.value = category
    return
  }
  activeCategory.value = category
  openedFaqId.value = faqs.find((item) => item.category === category)?.id || ''
}
function runSearch() {
  if (!keyword.value.trim()) {
    ElMessage.info('请输入要搜索的帮助内容')
    return
  }
  const first = filteredFaqs.value[0]
  if (first) openedFaqId.value = first.id
  else ElMessage.info('未找到匹配内容，可向智能客服继续咨询')
}
function useHotKeyword(value: string, category: HelpCategory) { keyword.value = value; activeCategory.value = category; runSearch() }
function runGuide(action: string) {
  if (action === 'orders') { void router.push('/customer/orders'); return }
  if (action === 'refund') { openService('我想申请退货退款，请告诉我需要准备的信息。'); return }
  if (action === 'invoice') { openService('我需要咨询发票申请问题。'); return }
  openService('我需要联系客服并咨询如何提交工单。')
}
function openTicket(ticketNo?: string) { void router.push({ path: '/customer/tickets', query: ticketNo ? { ticketNo } : {} }) }

/** 帮助页业务统计和最近会话独立容错，单个接口失败时保留其它真实内容。 */
async function loadHelpData(showFeedback = false) {
  if (refreshing.value) return
  refreshing.value = true
  const results = await Promise.allSettled([customerApi.orders(), customerApi.tickets(), sessionStore.refresh(true), customerApi.agentStatus()])
  let successCount = 0
  if (results[0].status === 'fulfilled') { orders.value = results[0].value.data; successCount += 1 }
  if (results[1].status === 'fulfilled') { tickets.value = results[1].value.data; successCount += 1 }
  if (results[2].status === 'fulfilled') successCount += 1
  if (results[3].status === 'fulfilled') { agentStatus.value = results[3].value.data; successCount += 1 }
  loaded.value = loaded.value || successCount > 0
  refreshing.value = false
  if (showFeedback && successCount) ElMessage.success('帮助中心内容已刷新')
  if (!successCount && !loaded.value) ElMessage.warning('业务数据暂不可用，帮助内容仍可正常查看')
}

onMounted(() => { void loadHelpData() })
onActivated(() => { if (!loaded.value) void loadHelpData() })
</script>

<template>
  <div class="customer-help-page">
    <CustomerSidebar :prototype-user="currentUser" :sessions="sessions" @contact-service="openService()" @unavailable="() => ElMessage.info('该功能暂未开放')" @select-session="openSession" @sessions-changed="() => sessionStore.refresh(true)" />

    <main class="customer-help-main">
      <header class="help-header customer-page-header">
        <div><h1>帮助中心</h1><p>搜索常见问题、查看操作指南、提交问题反馈与获取人工支持</p></div>
        <div class="help-header-actions customer-page-header-actions"><el-button :icon="Refresh" :loading="refreshing" @click="loadHelpData(true)">刷新内容</el-button><el-button :icon="EditPen" @click="openService('我想反馈帮助中心的问题或建议。')">问题反馈</el-button><el-button type="primary" :icon="ChatDotRound" @click="openService(undefined, true)">新建咨询</el-button></div>
      </header>

      <section class="help-workspace">
        <div class="help-center-column">
          <section class="help-search-hero">
            <div><h2>您好，有什么可以帮您？</h2><p>输入关键词搜索常见问题、订单帮助、售后指南与账户相关说明。</p><div class="help-search-box"><el-input v-model="keyword" :prefix-icon="Search" placeholder="搜索帮助内容 / 订单 / 退款 / 发票 / 物流" clearable @keyup.enter="runSearch" /><el-button type="primary" :icon="Search" aria-label="搜索" @click="runSearch" /></div><div class="help-hot-search"><span>热门搜索：</span><button @click="useHotKeyword('退货退款', 'afterSale')">退货退款</button><button @click="useHotKeyword('物流', 'order')">查看物流</button><button @click="useHotKeyword('发票', 'invoice')">发票申请</button><button @click="useHotKeyword('人工客服', 'account')">联系客服</button></div></div><div class="help-illustration" aria-hidden="true"><div class="help-monitor"><b>?</b></div><i>▰</i><span>● ●</span></div>
          </section>

          <section class="help-category-grid"><button v-for="category in categories" :key="category.key" :class="['help-category-card', category.tone, { active: activeCategory === category.key }]" @click="category.key === 'account' ? openService() : chooseCategory(category.key)"><el-icon><component :is="category.icon" /></el-icon><span><b>{{ category.title }}</b><small>{{ category.description }}</small><em>{{ category.action }}　›</em></span></button></section>

          <section class="help-metric-grid"><article v-for="metric in helpMetrics" :key="metric.label"><el-icon :class="metric.tone"><component :is="metric.icon" /></el-icon><span>{{ metric.label }}</span><b>{{ metric.value }}</b><small>{{ metric.note }}</small></article></section>

          <section class="help-content-grid">
            <article class="help-faq-card"><div class="help-card-head"><h2>热门问题</h2><div><button v-for="category in [{ key: 'all', label: '全部' }, { key: 'order', label: '订单' }, { key: 'afterSale', label: '售后' }, { key: 'invoice', label: '发票' }, { key: 'account', label: '账户' }]" :key="category.key" :class="{ active: activeCategory === category.key }" @click="chooseCategory(category.key as HelpCategory)">{{ category.label }}</button></div></div><div class="help-faq-list"><button v-for="faq in filteredFaqs" :key="faq.id" :class="{ open: openedFaqId === faq.id }" @click="openedFaqId = openedFaqId === faq.id ? '' : faq.id"><span><QuestionFilled /><b>{{ faq.question }}</b><em>{{ categoryLabels[faq.category] }}</em><i>⌄</i></span><p v-if="openedFaqId === faq.id">{{ faq.answer }}</p></button><el-empty v-if="!filteredFaqs.length" description="未找到匹配内容" :image-size="54" /></div><button class="help-card-all" @click="activeCategory = 'all'; keyword = ''">查看全部问题　›</button></article>
            <article class="help-guide-card"><div class="help-card-head"><h2>操作指南</h2></div><button v-for="guide in guides" :key="guide.title" @click="runGuide(guide.action)"><el-icon :class="guide.tone"><component :is="guide.icon" /></el-icon><span><b>{{ guide.title }}</b><small>{{ guide.description }}</small></span><em>查看详情</em></button><button class="help-card-all" @click="openService('我需要更多操作指南。')">查看全部指南　›</button></article>
          </section>

          <section class="orders-quick-services help-quick-services"><h2>快捷服务</h2><button @click="router.push('/customer/orders')"><Van /><span>查询物流</span><small>查看物流跟踪进度</small></button><button @click="openService('我想申请退货退款，请帮我确认需要提供的信息。')"><i>◉</i><span>申请退货退款</span><small>退货/退款申请入口</small></button><button @click="openService()"><Headset /><span>联系客服</span><small>联系在线智能客服</small></button><button @click="openService('我需要咨询发票问题。')"><i>▣</i><span>发票问题</span><small>发票申请与咨询</small></button></section>
        </div>

        <aside class="help-right-column">
          <section class="help-side-card"><h2>联系方式</h2><button @click="openService()"><i class="blue"><ChatDotRound /></i><span><b>智能客服</b><small>{{ agentOnline ? '7×24 小时在线' : '服务状态暂不可用' }}</small></span><em>立即咨询</em></button><button @click="openService('请帮我转人工客服。')"><i class="orange"><Service /></i><span><b>人工客服</b><small>通过智能客服申请接入</small></span><em>转人工</em></button><div class="help-contact-row"><i class="green"><CircleCheckFilled /></i><span><b>数据范围</b><small>仅展示当前登录客户数据</small></span></div></section>
          <section class="help-side-card help-common-entry"><h2>常用入口</h2><button @click="router.push('/customer/orders')"><Tickets /><span>我的订单</span><b>{{ orders.length }}</b></button><button @click="router.push('/customer/tickets')"><Document /><span>我的工单</span><b>{{ tickets.length }}</b></button><button @click="router.push('/customer/orders')"><Wallet /><span>售后订单</span><b>{{ afterSaleOrders }}</b></button><button @click="openService('我需要咨询发票问题。')"><QuestionFilled /><span>发票咨询</span></button><button class="help-side-all" @click="router.push('/customer')">返回服务首页　›</button></section>
          <section class="help-side-card help-recent-events"><h2>最近服务进度</h2><button v-for="event in recentEvents" :key="event.ticketNo" @click="openTicket(event.ticketNo)"><i></i><time>{{ event.time }}</time><p>{{ event.text }}</p></button><el-empty v-if="!recentEvents.length && loaded" description="暂无服务进度" :image-size="54" /><button class="help-side-all" @click="openTicket()">查看全部进度　›</button></section>
        </aside>
      </section>
    </main>
  </div>
</template>
