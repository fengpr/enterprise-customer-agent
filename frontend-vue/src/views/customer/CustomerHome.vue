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
  // SSE 建连和首个 token 之间立即显示可见状态，避免客户误以为页面卡死。
  lastReply.value = {
    session_id: selectedSessionId.value || 'pending',
    answer: '正在接收您的问题…',
    customer_message: '正在接收您的问题…',
    service_status: '请求已发送',
    auto_send: false,
    need_human: false
  }
  try {
    // 同一次提交在所有 SSE 重连中复用幂等键，避免重复执行模型或重复创建工单。
    const idempotencyKey = `customer-web:${crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`
    const requestPayload = {
      message: content,
      session_id: selectedSessionId.value || null,
      selected_order_no: selectedOrderNo.value || null,
      selected_ticket_no: selectedTicketNo.value || null,
      route_target: routeTarget.value
    }
    let streamedAnswer = ''
    let finalReply: AgentReply | null = null
    let requestId = ''
    let lastEventId = ''
    let terminalType = ''
    const reconnectDeadline = Date.now() + 120_000

    const handleStreamEvent = (event: { event_type: string; payload: Record<string, unknown>; event_id: string }) => {
        const stageMessages: Record<string, string> = {
          accepted: '已收到问题，正在准备处理…',
          queued: '请求已进入处理队列…',
          retrieving: '正在检索相关知识…',
          tool_calling: '正在查询相关业务信息…',
          generating: '已找到相关信息，正在组织回答…'
        }
        if (stageMessages[event.event_type] && !streamedAnswer) {
          const stageMessage = stageMessages[event.event_type]
          lastReply.value = {
            session_id: selectedSessionId.value || 'pending',
            answer: stageMessage,
            customer_message: stageMessage,
            service_status: stageMessage,
            auto_send: false,
            need_human: false
          }
        }
        if (event.event_type === 'delta') {
          streamedAnswer += String(event.payload.text || '')
          lastReply.value = {
            session_id: selectedSessionId.value || 'pending',
            answer: streamedAnswer,
            customer_message: streamedAnswer,
            auto_send: true,
            need_human: false
          }
        }
        if (event.event_type === 'completed' || event.event_type === 'degraded' || event.event_type === 'error') {
          const answer = String(event.payload.answer || event.payload.customer_message || streamedAnswer || '当前服务繁忙，请稍后查询处理进度。')
          lastReply.value = {
            session_id: selectedSessionId.value || 'pending',
            answer,
            customer_message: answer,
            service_status: String(event.payload.service_status || ''),
            auto_send: event.event_type === 'completed' && !Boolean(event.payload.degraded),
            need_human: event.event_type !== 'completed' || Boolean(event.payload.degraded)
          }
        }
    }

    // 后端单次 SSE 订阅有时限；连接正常结束但任务未完成时自动续传，并用状态查询兜底。
    while (!terminalType && Date.now() < reconnectDeadline) {
      try {
        const streamResult = await customerApi.streamReply(requestPayload, handleStreamEvent, {
          idempotencyKey,
          lastEventId: lastEventId || undefined
        })
        requestId = streamResult.requestId || requestId
        lastEventId = streamResult.lastEventId || lastEventId
        terminalType = streamResult.terminalType || terminalType
      } catch (error) {
        // 尚未获得 request_id 时无法安全续传，保留原异常交给外层恢复输入框。
        if (!requestId) throw error
      }

      if (requestId) {
        try {
          const { data: state } = await customerApi.replyResult(requestId)
          if (['SUCCESS', 'DEGRADED', 'FAILED', 'DEAD_LETTER'].includes(state.status)) {
            finalReply = state.result || null
            terminalType = state.status === 'SUCCESS' ? 'completed' : 'degraded'
            if (finalReply) {
              lastReply.value = finalReply
            }
            break
          }
        } catch {
          // 临时网络错误不应删除用户问题；下一轮仍用同一 request_id 和事件游标恢复。
        }
      }
      if (!terminalType) {
        lastReply.value = {
          session_id: selectedSessionId.value || 'pending',
          answer: streamedAnswer || '请求仍在后台处理中，正在继续等待结果…',
          customer_message: streamedAnswer || '请求仍在后台处理中，正在继续等待结果…',
          service_status: '后台处理中',
          auto_send: false,
          need_human: false
        }
        await new Promise((resolve) => window.setTimeout(resolve, 500))
      }
    }

    // 两分钟内仍未结束时保留临时消息和 request_id 状态，不能回刷成提问前的旧会话。
    if (!terminalType) {
      ElMessage.info('请求仍在后台处理中，可稍后刷新会话查看结果')
      return
    }

    // 终态事件可能先于查询响应到达，统一再读取一次持久化结果。
    if (requestId && !finalReply) {
      try {
        const { data: state } = await customerApi.replyResult(requestId)
        finalReply = state.result || null
      } catch {
        // SSE 已提供客户可见终态时，即使结果查询临时失败也保留该回复。
      }
    }
    const data = finalReply || lastReply.value
    if (!data) throw new Error('未收到客服回复')
    selectedSessionId.value = data.session_id || selectedSessionId.value
    const ticketChanged = mergeReplyTicket(data)
    if (finalReply) {
      await loadSessions(true)
    }
    if (ticketChanged) {
      // 只有建单或工单状态发生变化时才后台刷新，普通 AI 回复不再触发工单接口。
      void loadTickets().catch(() => undefined)
    }
    // 只有确认后端已持久化结果后才清理临时回复，避免 DLQ/网络异常让回答从页面消失。
    if (finalReply) {
      lastReply.value = null
    }
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
