<script setup lang="ts">
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { customerApi } from '@/api/customer'
import ChatAssistantPanel from '@/components/customer/ChatAssistantPanel.vue'
import CustomerSidebar from '@/components/customer/CustomerSidebar.vue'
import OrderCardList from '@/components/customer/OrderCardList.vue'
import TicketList from '@/components/customer/TicketList.vue'
import TicketProgressPanel from '@/components/customer/TicketProgressPanel.vue'
import { useAuthStore } from '@/stores/auth'
import type { AgentReply, AgentStatus, ChatMessage, ChatSession, CustomerOrder, RouteTarget, Ticket } from '@/types/api'

const auth = useAuthStore()
const agentStatus = ref<AgentStatus | null>(null)
const sessions = ref<ChatSession[]>([])
const messages = ref<ChatMessage[]>([])
const orders = ref<CustomerOrder[]>([])
const tickets = ref<Ticket[]>([])
const selectedSessionId = ref<string | null>(null)
const selectedOrderNo = ref<string | null>(null)
const selectedTicketNo = ref<string | null>(null)
const messageText = ref('')
const routeTarget = ref<RouteTarget>('ai')
const lastReply = ref<AgentReply | null>(null)
const loadingOrders = ref(false)
const loadingTickets = ref(false)
const loadingSessions = ref(false)
const loadingMessages = ref(false)
const submitting = ref(false)
const creatingSession = ref(false)
const lowerGridRef = ref<HTMLElement | null>(null)
const contentGridRef = ref<HTMLElement | null>(null)

const layoutStorageKey = 'customer-service-layout-v1'
const layoutState = ref({
  ordersCollapsed: false,
  ticketsCollapsed: false,
  progressCollapsed: false,
  ticketWidth: 300,
  progressWidth: 280
})

const selectedOrder = computed(() => orders.value.find((item) => item.orderNo === selectedOrderNo.value) ?? null)
const selectedTicket = computed(() => tickets.value.find((item) => item.ticketNo === selectedTicketNo.value) ?? null)
const selectedSession = computed(() => sessions.value.find((item) => item.session_id === selectedSessionId.value) ?? null)
const layoutStyle = computed(() => ({
  '--ticket-column-width': layoutState.value.ticketsCollapsed ? '56px' : `${layoutState.value.ticketWidth}px`,
  '--progress-column-width': layoutState.value.progressCollapsed ? '56px' : `${layoutState.value.progressWidth}px`
}))

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max)
}

function loadLayoutState() {
  try {
    const raw = localStorage.getItem(layoutStorageKey)
    if (!raw) return
    const saved = JSON.parse(raw) as Partial<typeof layoutState.value>
    layoutState.value = {
      ordersCollapsed: Boolean(saved.ordersCollapsed),
      ticketsCollapsed: Boolean(saved.ticketsCollapsed),
      progressCollapsed: Boolean(saved.progressCollapsed),
      ticketWidth: clamp(Number(saved.ticketWidth) || 300, 260, 420),
      progressWidth: clamp(Number(saved.progressWidth) || 280, 260, 380)
    }
  } catch {
    localStorage.removeItem(layoutStorageKey)
  }
}

function startTicketResize(event: MouseEvent) {
  if (layoutState.value.ticketsCollapsed) return
  event.preventDefault()
  const startX = event.clientX
  const startWidth = layoutState.value.ticketWidth
  const onMove = (moveEvent: MouseEvent) => {
    layoutState.value.ticketWidth = clamp(startWidth + moveEvent.clientX - startX, 260, 420)
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

function startProgressResize(event: MouseEvent) {
  if (layoutState.value.progressCollapsed) return
  event.preventDefault()
  const startX = event.clientX
  const startWidth = layoutState.value.progressWidth
  const onMove = (moveEvent: MouseEvent) => {
    layoutState.value.progressWidth = clamp(startWidth - (moveEvent.clientX - startX), 260, 380)
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

watch(
  layoutState,
  (value) => {
    localStorage.setItem(layoutStorageKey, JSON.stringify(value))
  },
  { deep: true }
)

async function loadAgentStatus() {
  const { data } = await customerApi.agentStatus()
  agentStatus.value = data
}

async function loadOrders() {
  loadingOrders.value = true
  try {
    const { data } = await customerApi.orders()
    orders.value = data
    if (selectedOrderNo.value && !data.some((item) => item.orderNo === selectedOrderNo.value)) {
      selectedOrderNo.value = null
    }
  } finally {
    loadingOrders.value = false
  }
}

async function loadTickets() {
  loadingTickets.value = true
  try {
    const { data } = await customerApi.tickets()
    tickets.value = data
    if (!selectedTicketNo.value && data.length) {
      selectedTicketNo.value = data[0].ticketNo
    }
  } finally {
    loadingTickets.value = false
  }
}

function mergeReplyTicket(reply: AgentReply) {
  const ticket = reply.ticket_result?.data
  if (reply.ticket_result?.status !== 'success' || !ticket?.ticketNo) {
    return false
  }

  // 回复已携带最新工单时先更新本地列表，后台校准期间页面不会短暂显示旧状态。
  const ticketIndex = tickets.value.findIndex((item) => item.ticketNo === ticket.ticketNo)
  if (ticketIndex >= 0) {
    tickets.value = tickets.value.map((item, index) => (index === ticketIndex ? { ...item, ...ticket } : item))
  } else {
    tickets.value = [ticket, ...tickets.value]
  }
  return true
}

async function loadSessions(keepSelection = true) {
  loadingSessions.value = true
  try {
    const { data } = await customerApi.sessions()
    sessions.value = data
    if (!keepSelection || !selectedSessionId.value) {
      selectedSessionId.value = data[0]?.session_id ?? null
    }
    if (selectedSessionId.value) {
      await loadMessages(selectedSessionId.value)
    } else {
      messages.value = []
    }
  } finally {
    loadingSessions.value = false
  }
}

async function loadMessages(sessionId: string) {
  loadingMessages.value = true
  try {
    const { data } = await customerApi.sessionDetail(sessionId)
    messages.value = data.messages
  } finally {
    loadingMessages.value = false
  }
}

async function selectSession(sessionId: string) {
  if (selectedSessionId.value === sessionId) {
    return
  }
  selectedSessionId.value = sessionId
  lastReply.value = null
  await loadMessages(sessionId)
}

async function createNewSession() {
  creatingSession.value = true
  try {
    const { data } = await customerApi.createSession()
    selectedSessionId.value = data.session_id
    sessions.value = [data, ...sessions.value]
    messages.value = []
    lastReply.value = null
    messageText.value = ''
    ElMessage.success('已新建咨询')
  } finally {
    creatingSession.value = false
  }
}

async function refreshTickets() {
  await loadTickets()
  ElMessage.success('工单已刷新')
}

function showReplySubmitFeedback(reply: AgentReply) {
  const ticket = reply.ticket_result?.data
  if (reply.ticket_result?.status === 'success') {
    ElMessage.success(ticket?.ticketNo ? `已创建工单 ${ticket.ticketNo}` : '工单已提交')
    return
  }
  if (reply.service_status && reply.service_status !== '自动回复') {
    ElMessage.success(reply.service_status)
    return
  }
  if (reply.need_human) {
    // 人工接管有工作时间外、排队和等待接入等状态，后端未返回细分状态时才使用兜底文案。
    ElMessage.success('已记录人工服务请求')
    return
  }
  // 普通知识咨询或基础能力介绍已经在聊天气泡中展示，不再弹“问题已提交”误导用户。
}

async function submitQuestion() {
  if (submitting.value) {
    return
  }
  const content = messageText.value.trim()
  if (!content) {
    ElMessage.warning('请先输入问题')
    return
  }
  const tempMessageId = -Date.now()
  submitting.value = true
  messageText.value = ''
  messages.value = [
    ...messages.value,
    {
      id: tempMessageId,
      session_id: selectedSessionId.value || 'pending',
      sender_type: 'customer',
      sender_id: null,
      content,
      message_type: 'text',
      extra_data: { route_target: routeTarget.value },
      created_at: new Date().toISOString()
    }
  ]
  try {
    const { data } = await customerApi.reply(
      content,
      selectedSessionId.value,
      selectedOrderNo.value,
      selectedTicketNo.value,
      routeTarget.value
    )
    lastReply.value = data
    selectedSessionId.value = data.session_id
    const ticketChanged = mergeReplyTicket(data)
    await loadSessions(true)
    if (ticketChanged) {
      // 只有建单或工单状态发生变化时才后台刷新，普通 AI 回复不再触发工单接口。
      void loadTickets().catch(() => undefined)
    }
    lastReply.value = null
    // 会话消息已经从后端重新加载后，不再额外叠加临时回复，避免聊天窗口出现重复 AI 消息。
    lastReply.value = null
    showReplySubmitFeedback(data)
  } catch (error) {
    messages.value = messages.value.filter((item) => item.id !== tempMessageId)
    messageText.value = content
    throw error
  } finally {
    submitting.value = false
  }
}

function selectOrder(orderNo: string) {
  if (selectedOrderNo.value === orderNo) {
    selectedOrderNo.value = null
    selectedTicketNo.value = null
    ElMessage.info('已取消当前咨询订单')
    return
  }
  selectedOrderNo.value = orderNo
  selectedTicketNo.value = null
  ElMessage.success(`已选择订单 ${orderNo}`)
}

function selectTicket(ticketNo: string) {
  selectedTicketNo.value = ticketNo
}

function fillAndMaybeSend(action: string) {
  if (action === '查询物流') {
    if (!selectedOrder.value) {
      ElMessage.warning('请先选择要查询物流的订单')
      return
    }
    messageText.value = `请帮我查询订单 ${selectedOrder.value.orderNo} 的物流`
    return
  }
  if (action === '申请退货') {
    if (!selectedOrder.value) {
      ElMessage.warning('请先选择要申请售后的订单')
      return
    }
    messageText.value = `我要退货，订单 ${selectedOrder.value.orderNo}，原因是需要补充`
    return
  }
  if (action === '催办工单') {
    if (!selectedTicket.value) {
      ElMessage.warning('请先选择需要催办的工单')
      return
    }
    messageText.value = `请帮我催办工单 ${selectedTicket.value.ticketNo}`
    void submitQuestion()
    return
  }
  if (action === '发票问题') {
    messageText.value = selectedOrder.value ? `订单 ${selectedOrder.value.orderNo} 我想咨询发票问题` : '我想咨询发票问题'
    return
  }
  messageText.value = '请帮我转人工客服'
  routeTarget.value = 'ai'
}

onMounted(async () => {
  loadLayoutState()
  await Promise.all([loadAgentStatus(), loadOrders(), loadTickets(), loadSessions(false)])
})
</script>

<template>
  <div class="customer-service-page">
    <CustomerSidebar
      :selected-session-id="selectedSessionId"
      :sessions="sessions"
      :user="auth.user"
      @select-session="selectSession"
    />

    <main class="customer-main">
      <header class="customer-hero">
        <div>
          <h1>客户自助服务</h1>
          <p>
            选择订单、提交咨询、查看工单、跟踪进度，一站式自助服务体验
            <span v-if="agentStatus?.llm?.enabled"> · LLM {{ agentStatus.llm.provider }}</span>
          </p>
        </div>
        <div class="hero-actions">
          <el-button :icon="Refresh" :loading="loadingOrders" @click="loadOrders">刷新订单</el-button>
          <el-button :icon="Refresh" :loading="loadingTickets" @click="refreshTickets">刷新工单</el-button>
          <el-button :icon="Plus" :loading="creatingSession" type="primary" @click="createNewSession">新建咨询</el-button>
        </div>
      </header>

      <div
        ref="contentGridRef"
        class="customer-content-grid"
        :class="{ 'is-progress-collapsed': layoutState.progressCollapsed }"
        :style="layoutStyle"
      >
        <section class="center-workspace">
          <OrderCardList
            :collapsed="layoutState.ordersCollapsed"
            :loading="loadingOrders"
            :orders="orders"
            :selected-order-no="selectedOrderNo"
            @after-sale="(orderNo) => { selectedOrderNo = orderNo; selectedTicketNo = null; fillAndMaybeSend('申请退货') }"
            @contact="(orderNo) => { selectedOrderNo = orderNo; selectedTicketNo = null; messageText = `关于订单 ${orderNo}：` }"
            @select="selectOrder"
            @toggle-collapse="layoutState.ordersCollapsed = !layoutState.ordersCollapsed"
          />

          <div
            ref="lowerGridRef"
            class="lower-grid"
            :class="{ 'is-tickets-collapsed': layoutState.ticketsCollapsed }"
            :style="layoutStyle"
          >
            <TicketList
              :collapsed="layoutState.ticketsCollapsed"
              :loading="loadingTickets"
              :selected-ticket-no="selectedTicketNo"
              :tickets="tickets"
              @select="selectTicket"
              @toggle-collapse="layoutState.ticketsCollapsed = !layoutState.ticketsCollapsed"
            />
            <button
              :class="['resize-handle', 'ticket-resize-handle', { disabled: layoutState.ticketsCollapsed }]"
              aria-label="调整工单列表宽度"
              type="button"
              @mousedown="startTicketResize"
            />
            <ChatAssistantPanel
              v-model="messageText"
              :last-reply="lastReply"
              :messages="messages"
              :route-target="routeTarget"
              :session="selectedSession"
              :selected-order="selectedOrder"
              :selected-ticket="selectedTicket"
              :submitting="submitting || loadingMessages"
              @continue-ai="routeTarget = 'ai'"
              @quick="fillAndMaybeSend"
              @update:route-target="routeTarget = $event"
              @submit="submitQuestion"
            />
          </div>
        </section>

        <button
          :class="['resize-handle', 'progress-resize-handle', { disabled: layoutState.progressCollapsed }]"
          aria-label="调整工单进度宽度"
          type="button"
          @mousedown="startProgressResize"
        />
        <TicketProgressPanel
          :collapsed="layoutState.progressCollapsed"
          :ticket="selectedTicket"
          @toggle-collapse="layoutState.progressCollapsed = !layoutState.progressCollapsed"
        />
      </div>
    </main>
  </div>
</template>
