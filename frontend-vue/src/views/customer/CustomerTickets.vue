<script setup lang="ts">
defineOptions({ name: 'CustomerTickets' })

import { ChatDotRound, CircleCheckFilled, Clock, Document, EditPen, Headset, Refresh, Search, Tickets, Van } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { storeToRefs } from 'pinia'
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { customerApi } from '@/api/customer'
import CustomerSidebar from '@/components/customer/CustomerSidebar.vue'
import { useCustomerSessionStore } from '@/stores/customerSessions'
import type { Ticket } from '@/types/api'

type StatusGroup = 'ALL' | 'PROCESSING' | 'SUPPLEMENT' | 'ASSIGN' | 'COMPLETED' | 'CLOSED'
type TicketTone = 'processing' | 'supplement' | 'assign' | 'completed' | 'closed'

const router = useRouter()
const route = useRoute()
const sessionStore = useCustomerSessionStore()
const { sessions } = storeToRefs(sessionStore)
const tickets = ref<Ticket[]>([])
const selectedTicketNo = ref<string | null>(null)
const selectedDetail = ref<Ticket | null>(null)
const loading = ref(false)
const loadingDetail = ref(false)
const urgingTicketNo = ref<string | null>(null)
const keyword = ref('')
const activeStatus = ref<StatusGroup>('ALL')
const selectedType = ref('ALL')
const dateRange = ref<[string, string] | null>(null)
const page = ref(1)
// 工单分页固定为每页 5 条，保证桌面原型的信息密度与翻页行为稳定。
const pageSize = 5

const statusTabs: Array<{ value: StatusGroup; label: string }> = [
  { value: 'ALL', label: '全部' },
  { value: 'PROCESSING', label: '处理中' },
  { value: 'SUPPLEMENT', label: '待补充' },
  { value: 'ASSIGN', label: '待分派' },
  { value: 'COMPLETED', label: '已完成' },
  { value: 'CLOSED', label: '已关闭' }
]

/** 将后端状态收敛为客户可理解的展示状态，同时兼容未来新增状态。 */
function displayStatus(ticket: Ticket): { key: StatusGroup; label: string; tone: TicketTone } {
  const status = ticket.status || 'PENDING_ASSIGN'
  if (status === 'PENDING_ASSIGN') return { key: 'ASSIGN', label: '待分派', tone: 'assign' }
  if (['WAITING_SUPPLEMENT', 'PENDING_SUPPLEMENT'].includes(status)) return { key: 'SUPPLEMENT', label: '待补充', tone: 'supplement' }
  if (['CANCELLED', 'REJECTED'].includes(status)) return { key: 'CLOSED', label: '已关闭', tone: 'closed' }
  if (status === 'CLOSED') return { key: 'COMPLETED', label: '已完成', tone: 'completed' }
  if (['PENDING_PROCESS', 'PROCESSING', 'TRANSFERRED', 'REOPENED'].includes(status)) return { key: 'PROCESSING', label: '处理中', tone: 'processing' }
  return { key: 'PROCESSING', label: status, tone: 'processing' }
}

const ticketTypes = computed(() => Array.from(new Set(tickets.value.map((item) => item.ticketType).filter(Boolean) as string[])))
const filteredTickets = computed(() => tickets.value.filter((ticket) => {
  const text = `${ticket.ticketNo} ${ticket.title || ''} ${ticket.content || ''} ${ticket.orderNo || ''}`.toLowerCase()
  const createdDate = ticket.createdAt?.slice(0, 10) || ''
  const matchedKeyword = !keyword.value.trim() || text.includes(keyword.value.trim().toLowerCase())
  const matchedStatus = activeStatus.value === 'ALL' || displayStatus(ticket).key === activeStatus.value
  const matchedType = selectedType.value === 'ALL' || ticket.ticketType === selectedType.value
  const matchedDate = !dateRange.value || (createdDate >= dateRange.value[0] && createdDate <= dateRange.value[1])
  return matchedKeyword && matchedStatus && matchedType && matchedDate
}))
const pageTickets = computed(() => filteredTickets.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const metrics = computed(() => ({
  total: tickets.value.length,
  processing: tickets.value.filter((item) => displayStatus(item).key === 'PROCESSING').length,
  supplement: tickets.value.filter((item) => displayStatus(item).key === 'SUPPLEMENT').length,
  completed: tickets.value.filter((item) => displayStatus(item).key === 'COMPLETED').length
}))

function formatTime(value?: string | null) { return value ? value.replace('T', ' ').slice(0, 16) : '—' }
function ticketTypeLabel(value?: string | null) {
  const labels: Record<string, string> = { RETURN: '退货申请', REFUND: '退款申请', COMPLAINT: '投诉反馈', INVOICE: '发票问题', LOGISTICS: '物流异常', TECH_SUPPORT: '技术支持', SERVICE: '服务工单', GENERAL: '综合咨询' }
  return value ? labels[value] || value : '综合咨询'
}
function resetFilters() { keyword.value = ''; activeStatus.value = 'ALL'; selectedType.value = 'ALL'; dateRange.value = null; page.value = 1 }
function selectStatus(value: StatusGroup) { activeStatus.value = value; page.value = 1 }
function isClosed(ticket: Ticket) { return ['CLOSED', 'CANCELLED', 'REJECTED'].includes(ticket.status || '') }
function needsSupplement(ticket: Ticket) { return ['WAITING_SUPPLEMENT', 'PENDING_SUPPLEMENT'].includes(ticket.status || '') }

/** 清理问题描述中的结构化内部标签，客户页只保留用户原始诉求。 */
function safeContent(value?: string | null) {
  if (!value) return '暂无问题描述'
  return value
    .replace(/(?:客户诉求|业务动作|用户目的|已收集信息|订单上下文)\s*[：:]\s*/g, '')
    .replace(/\s*\|\s*/g, '；')
    .trim() || '暂无问题描述'
}

function expectedResolution(ticket: Ticket) {
  const type = `${ticket.ticketType || ''} ${ticket.title || ''}`.toUpperCase()
  if (type.includes('RETURN') || type.includes('退货')) return '核验退货条件并协助安排退货处理。'
  if (type.includes('REFUND') || type.includes('退款')) return '核验订单与支付信息并反馈退款处理进度。'
  if (type.includes('INVOICE') || type.includes('发票')) return '核验开票信息并协助处理发票诉求。'
  if (type.includes('LOGISTICS') || type.includes('物流')) return '核查物流轨迹并反馈后续处理方案。'
  if (type.includes('COMPLAINT') || type.includes('投诉')) return '核实问题并给出明确的处理反馈。'
  return '核实客户问题并提供可执行的处理方案。'
}

function statusExplanation(ticket: Ticket) {
  const status = displayStatus(ticket)
  if (status.key === 'ASSIGN') return '工单已提交，正在等待客服人员分派处理。'
  if (status.key === 'SUPPLEMENT') return '客服需要您补充必要信息，请继续沟通并提交材料。'
  if (status.key === 'COMPLETED') return '工单处理已完成，如仍有疑问可继续联系客服。'
  if (status.key === 'CLOSED') return '该工单已关闭，如需继续处理请发起新的咨询。'
  return '客服正在处理您的申请，请留意后续进度更新。'
}

const timeline = computed(() => {
  const ticket = selectedDetail.value
  if (!ticket) return []
  const status = displayStatus(ticket).key
  const accepted = Boolean(ticket.assignedAt) || !['ASSIGN'].includes(status)
  const completed = status === 'COMPLETED' || status === 'CLOSED'
  const processing = status === 'PROCESSING' || status === 'SUPPLEMENT' || completed
  return [
    { label: '已提交', desc: '您的工单申请已记录', time: ticket.createdAt, state: 'done' },
    { label: '已受理', desc: accepted ? '客服已受理您的工单' : '等待客服人员受理', time: ticket.assignedAt, state: accepted ? 'done' : 'waiting' },
    { label: status === 'SUPPLEMENT' ? '待补充' : '处理中', desc: status === 'SUPPLEMENT' ? '等待您补充必要信息' : '客服正在为您处理', time: !completed && processing ? ticket.updatedAt : null, state: !completed && processing ? 'active' : processing ? 'done' : 'waiting' },
    { label: '待确认', desc: completed ? '处理结果已形成' : '等待处理结果确认', time: null, state: completed ? 'done' : 'waiting' },
    { label: '已完成', desc: completed ? '工单处理流程已结束' : '处理完成后将在此更新', time: completed ? ticket.updatedAt : null, state: completed ? 'done' : 'waiting' }
  ]
})

function openService(options: { ticket?: Ticket; message?: string; newSession?: boolean } = {}) {
  const query: Record<string, string> = {}
  if (options.newSession !== false) query.new = '1'
  if (options.ticket?.ticketNo) query.ticketNo = options.ticket.ticketNo
  if (options.message) query.message = options.message
  void router.push({ path: '/customer/service', query })
}

function openSession(sessionId: string) { void router.push({ path: '/customer/service', query: { sessionId } }) }
function createTicket() { openService({ message: '我想创建一个新的服务工单，请引导我补充必要信息。' }) }
function supplementTicket(ticket: Ticket) { openService({ ticket, message: `我需要为工单 ${ticket.ticketNo} 补充申请材料。` }) }

/** 详情单独校准，失败时保留上一次成功内容，避免右栏闪白。 */
async function selectTicket(ticketNo: string, force = false) {
  if (!force && selectedTicketNo.value === ticketNo && selectedDetail.value) return
  selectedTicketNo.value = ticketNo
  loadingDetail.value = true
  try {
    const result = await customerApi.ticket(ticketNo)
    selectedDetail.value = result.data
  } catch {
    const fallback = tickets.value.find((item) => item.ticketNo === ticketNo)
    if (!selectedDetail.value && fallback) selectedDetail.value = fallback
  } finally {
    loadingDetail.value = false
  }
}

async function loadSessions() {
  try { await sessionStore.refresh(true) } catch { /* 侧栏保留上次成功数据。 */ }
}

async function loadPage(showFeedback = false) {
  if (loading.value) return
  loading.value = true
  try {
    const [ticketResult] = await Promise.all([customerApi.tickets(), sessionStore.refresh(true)])
    tickets.value = ticketResult.data
    // 首页携带工单号时仅在当前客户的工单集合中定位，避免信任外部 URL 参数。
    const targetTicketNo = typeof route.query.ticketNo === 'string' ? route.query.ticketNo : selectedTicketNo.value
    const target = tickets.value.find((item) => item.ticketNo === targetTicketNo) || tickets.value[0]
    if (target) await selectTicket(target.ticketNo, true)
    else { selectedTicketNo.value = null; selectedDetail.value = null }
    if (showFeedback) ElMessage.success('工单数据已刷新')
  } catch {
    // 接口错误已统一提示；页面继续展示上一次成功结果。
  } finally {
    loading.value = false
  }
}

async function urgeTicket(ticket: Ticket) {
  try {
    await ElMessageBox.confirm(`确认催办工单 ${ticket.ticketNo} 吗？客服将看到本次催办记录。`, '催办工单', { confirmButtonText: '确认催办', cancelButtonText: '取消', type: 'warning' })
    urgingTicketNo.value = ticket.ticketNo
    const result = await customerApi.urgeTicket(ticket.ticketNo, '客户在我的工单页面发起催办')
    const index = tickets.value.findIndex((item) => item.ticketNo === ticket.ticketNo)
    if (index >= 0) tickets.value[index] = result.data
    if (selectedTicketNo.value === ticket.ticketNo) selectedDetail.value = result.data
    ElMessage.success('已提交催办，请留意后续进度')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') return
  } finally {
    urgingTicketNo.value = null
  }
}

watch([keyword, activeStatus, selectedType, dateRange], () => { page.value = 1 })
watch(() => route.query.ticketNo, (ticketNo) => {
  if (typeof ticketNo === 'string' && tickets.value.some((item) => item.ticketNo === ticketNo)) void selectTicket(ticketNo)
})
watch(pageTickets, (rows) => {
  if (rows.length && !rows.some((item) => item.ticketNo === selectedTicketNo.value)) void selectTicket(rows[0].ticketNo)
})
onMounted(() => { void loadPage() })
onActivated(() => { if (!tickets.value.length) void loadPage() })
</script>

<template>
  <div class="customer-tickets-page">
    <CustomerSidebar :sessions="sessions" :selected-session-id="null" @contact-service="openService()" @unavailable="() => ElMessage.info('该功能页面暂未开放')" @select-session="openSession" @sessions-changed="loadSessions" />

    <main class="customer-tickets-main">
      <header class="tickets-header customer-page-header">
        <div><h1>我的工单</h1><p>查看工单状态、处理进度、沟通记录与服务详情</p></div>
        <div class="tickets-header-actions customer-page-header-actions"><el-button :icon="Refresh" :loading="loading" @click="loadPage(true)">刷新工单</el-button><el-button :icon="EditPen" @click="createTicket">创建工单</el-button><el-button type="primary" :icon="ChatDotRound" @click="openService()">新建咨询</el-button></div>
      </header>

      <section class="ticket-metric-grid">
        <article><i class="blue"><Tickets /></i><span>全部工单</span><b>{{ metrics.total }}</b><small>当前账户全部工单</small></article>
        <article><i class="orange"><Clock /></i><span>处理中</span><b>{{ metrics.processing }}</b><small>正在为您处理的工单</small></article>
        <article><i class="purple"><Document /></i><span>待补充</span><b>{{ metrics.supplement }}</b><small>等待补充材料的工单</small></article>
        <article><i class="green"><CircleCheckFilled /></i><span>已完成</span><b>{{ metrics.completed }}</b><small>已完成处理的工单</small></article>
      </section>

      <section class="tickets-workspace">
        <div class="tickets-left">
          <section class="tickets-filter-card">
            <div class="tickets-filter-top"><el-input v-model="keyword" :prefix-icon="Search" placeholder="搜索工单号 / 关键词" clearable /><div class="ticket-status-tabs"><button v-for="tab in statusTabs" :key="tab.value" :class="{ active: activeStatus === tab.value }" @click="selectStatus(tab.value)">{{ tab.label }}</button></div></div>
            <div class="tickets-filter-bottom"><span>时间范围</span><el-date-picker v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" /><span>工单类型</span><el-select v-model="selectedType"><el-option label="全部类型" value="ALL" /><el-option v-for="type in ticketTypes" :key="type" :label="ticketTypeLabel(type)" :value="type" /></el-select><div class="tickets-filter-actions"><el-button @click="resetFilters">重置</el-button><el-button type="primary" @click="page = 1">筛选</el-button></div></div>
          </section>

          <section class="tickets-list-card" v-loading="loading">
            <h2>工单列表 <small>（共 {{ filteredTickets.length }} 个工单）</small></h2>
            <div class="ticket-table-scroll">
              <div class="customer-ticket-table customer-ticket-table-head"><span></span><span>工单号</span><span>工单类型</span><span>关联订单</span><span>提交时间</span><span>当前状态</span><span>最近更新时间</span><span>操作</span></div>
              <div v-for="ticket in pageTickets" :key="ticket.ticketNo" :class="['customer-ticket-table', 'customer-ticket-table-row', { selected: selectedTicketNo === ticket.ticketNo }]" role="button" tabindex="0" @click="selectTicket(ticket.ticketNo)" @keydown.enter="selectTicket(ticket.ticketNo)">
                <span class="customer-ticket-radio"><i></i></span><b>{{ ticket.ticketNo }}</b><span>{{ ticketTypeLabel(ticket.ticketType) }}</span><span>{{ ticket.orderNo || '—' }}</span><span>{{ formatTime(ticket.createdAt) }}</span><em :class="displayStatus(ticket).tone">{{ displayStatus(ticket).label }}</em><span>{{ formatTime(ticket.updatedAt) }}</span>
                <span class="customer-ticket-row-actions"><el-button size="small" @click.stop="selectTicket(ticket.ticketNo)">查看详情</el-button><el-button v-if="needsSupplement(ticket)" size="small" type="primary" plain @click.stop="supplementTicket(ticket)">补充材料</el-button><el-button v-else-if="!isClosed(ticket)" size="small" type="primary" plain :loading="urgingTicketNo === ticket.ticketNo" @click.stop="urgeTicket(ticket)">催办工单</el-button></span>
              </div>
            </div>
            <el-empty v-if="!pageTickets.length" description="没有匹配的工单" :image-size="68" />
            <div class="tickets-pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" layout="prev, pager, next" :total="filteredTickets.length" /></div>
          </section>

          <section class="orders-quick-services tickets-quick-services"><h2>快捷服务</h2><button @click="router.push('/customer/orders')"><Van /><span>查询物流</span><small>查看物流跟踪进度</small></button><button @click="openService({ ticket: selectedDetail || undefined, message: '我想申请退货退款，请帮我确认需要提供的信息。' })"><i>◉</i><span>申请退货退款</span><small>退货/退款申请入口</small></button><button @click="openService({ ticket: selectedDetail || undefined })"><Headset /><span>联系客服</span><small>联系在线智能客服</small></button><button @click="openService({ ticket: selectedDetail || undefined, message: '我需要咨询发票问题。' })"><i>▣</i><span>发票问题</span><small>发票申请与咨询</small></button></section>
        </div>

        <aside class="ticket-detail-panel" v-loading="loadingDetail">
          <template v-if="selectedDetail">
            <h2>工单详情</h2>
            <div class="ticket-detail-id"><b>{{ selectedDetail.ticketNo }}</b><em :class="displayStatus(selectedDetail).tone">{{ displayStatus(selectedDetail).label }}</em></div>
            <p><span>关联订单</span>{{ selectedDetail.orderNo || '暂无关联订单' }}</p><p><span>提交时间</span>{{ formatTime(selectedDetail.createdAt) }}</p><p><span>更新时间</span>{{ formatTime(selectedDetail.updatedAt) }}</p>
            <section class="ticket-detail-section"><div v-for="step in timeline" :key="step.label" :class="['ticket-timeline-step', step.state]"><i><CircleCheckFilled v-if="step.state === 'done'" /></i><div><b>{{ step.label }}</b><time v-if="step.time">{{ formatTime(step.time) }}</time><p>{{ step.desc }}</p></div></div></section>
            <section class="ticket-detail-section ticket-safe-fields"><p><span>问题类型</span>{{ ticketTypeLabel(selectedDetail.ticketType) }}</p><p><span>问题描述</span>{{ safeContent(selectedDetail.content || selectedDetail.title) }}</p><p><span>期望解决</span>{{ expectedResolution(selectedDetail) }}</p><p><span>附件</span>暂无客户上传附件</p><p><span>催办记录</span>{{ selectedDetail.urgeCount ? `已催办 ${selectedDetail.urgeCount} 次，最近 ${formatTime(selectedDetail.lastUrgedAt)}` : '暂无催办记录' }}</p><template v-if="selectedDetail.returnMethod || selectedDetail.pickupTimeWindow || selectedDetail.pickupStatus"><p><span>退货方式</span>{{ selectedDetail.returnMethod || '暂无' }}</p><p><span>取件时间</span>{{ selectedDetail.pickupTimeWindow || '暂无' }}</p><p><span>履约状态</span>{{ selectedDetail.pickupStatus || '暂无' }}</p></template><p><span>当前说明</span>{{ statusExplanation(selectedDetail) }}</p></section>
            <el-button class="ticket-detail-contact" type="primary" plain @click="openService({ ticket: selectedDetail, newSession: true })">查看工单详情</el-button>
          </template>
          <el-empty v-else description="请选择一个工单" />
        </aside>
      </section>
    </main>
  </div>
</template>
