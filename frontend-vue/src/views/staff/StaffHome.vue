<script setup lang="ts">
import { Check, Close, Refresh, SwitchButton, UserFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { staffHandoffApi, staffReplyApi, staffTicketApi } from '@/api/staff'
import { useAuthStore } from '@/stores/auth'
import type { ChatMessage, StaffHandoffDetail, StaffHandoffSession, Ticket } from '@/types/api'
import { statusType } from '@/utils/ticket'

const router = useRouter()
const auth = useAuthStore()
const tickets = ref<Ticket[]>([])
const handoffSessions = ref<StaffHandoffSession[]>([])
const handoffMessages = ref<ChatMessage[]>([])
// 座席默认只读取交接摘要与最近相关窗口，更早历史必须由座席主动展开。
const handoffSummary = ref<StaffHandoffDetail['handoff_summary'] | null>(null)
const handoffHistoryAvailable = ref(false)
const handoffHistoryAccessAllowed = ref(false)
const handoffHistoryLoading = ref(false)
const selectedTicketNo = ref<string | null>(null)
const selectedHandoffSessionId = ref<string | null>(null)
const loading = ref(false)
const handoffLoading = ref(false)
const acting = ref(false)
const generatingDraft = ref(false)
const closeReason = ref('问题已处理完成')
const draftMessage = ref('')
const handoffReply = ref('')
const handoffCloseMessage = ref('人工服务已结束，后续可继续由智能助手协助。')
const workMode = ref<'tickets' | 'handoff'>('tickets')
const handoffScope = ref<'pending' | 'mine'>('pending')
const latestHandoffMessageId = ref(0)
const seenHandoffMessageIds = ref<Record<string, number>>({})
let queuePollTimer: number | null = null
let detailPollTimer: number | null = null
let heartbeatTimer: number | null = null
let handoffPolling = false
let queuePollFailures = 0
let detailPollFailures = 0
let handoffDetailRequestVersion = 0
const queueScope = ref<'mine' | 'claimable'>('mine')
const selectedStatuses = ref(['PENDING_ASSIGN', 'PENDING_PROCESS', 'PROCESSING', 'REOPENED'])

const statusOptions = ['PENDING_ASSIGN', 'PENDING_PROCESS', 'PROCESSING', 'REOPENED', 'CLOSED']
const selectedTicket = computed(() => tickets.value.find((item) => item.ticketNo === selectedTicketNo.value) ?? null)
const selectedHandoff = computed(() => handoffSessions.value.find((item) => item.session_id === selectedHandoffSessionId.value) ?? null)
const visibleHandoffs = computed(() => handoffSessions.value.filter((item) => handoffScope.value === 'pending' ? item.handoff_status === 'PENDING' : item.handoff_status === 'ACTIVE'))
const visibleTickets = computed(() => {
  if (queueScope.value === 'claimable') {
    return tickets.value.filter((ticket) => !ticket.handlerId || ticket.status === 'PENDING_ASSIGN')
  }
  return tickets.value.filter((ticket) => ticket.handlerId === auth.user?.user_id)
})
const canClaim = computed(() => {
  return Boolean(selectedTicket.value && !selectedTicket.value.handlerId && selectedTicket.value.status === 'PENDING_ASSIGN')
})
const canOperate = computed(() => selectedTicket.value?.handlerId === auth.user?.user_id)
const canOperateHandoff = computed(() => {
  return selectedHandoff.value?.handoff_status === 'ACTIVE' && selectedHandoff.value.human_assigned_staff_id === String(auth.user?.user_id)
})

async function loadTickets() {
  loading.value = true
  try {
    const { data } = await staffTicketApi.list(selectedStatuses.value.join(','))
    tickets.value = data
    if (!selectedTicketNo.value || !data.some((item) => item.ticketNo === selectedTicketNo.value)) {
      selectedTicketNo.value = visibleTickets.value[0]?.ticketNo ?? null
    }
  } finally {
    loading.value = false
  }
}

async function loadHandoffSessions(keepSelection = true) {
  handoffLoading.value = true
  try {
    const previousSelection = selectedHandoffSessionId.value
    const { data } = await staffHandoffApi.list()
    handoffSessions.value = data
    if (!keepSelection || !selectedHandoffSessionId.value || !data.some((item) => item.session_id === selectedHandoffSessionId.value)) {
      selectedHandoffSessionId.value = data[0]?.session_id ?? null
    }
    if (selectedHandoffSessionId.value && (previousSelection !== selectedHandoffSessionId.value || !handoffMessages.value.length)) {
      await loadHandoffDetail(selectedHandoffSessionId.value)
    } else if (!selectedHandoffSessionId.value) {
      // 只有当前会话被移出队列或不再可见时才清理历史，刷新同一会话不能覆盖本地消息。
      handoffMessages.value = []
      handoffSummary.value = null
      handoffHistoryAvailable.value = false
      handoffHistoryAccessAllowed.value = false
      latestHandoffMessageId.value = 0
    }
  } finally {
    handoffLoading.value = false
  }
}

watch(handoffScope, () => {
  const first = visibleHandoffs.value[0]
  if (first && !visibleHandoffs.value.some((item) => item.session_id === selectedHandoffSessionId.value)) {
    void selectHandoff(first)
  }
})

async function loadHandoffDetail(sessionId: string) {
  const requestVersion = ++handoffDetailRequestVersion
  const { data } = await staffHandoffApi.detail(sessionId)
  // 用户在网络请求期间切换了会话时，旧响应不能覆盖新会话的消息历史。
  if (selectedHandoffSessionId.value !== sessionId || requestVersion !== handoffDetailRequestVersion) return
  handoffMessages.value = data.messages
  handoffSummary.value = data.handoff_summary ?? null
  handoffHistoryAvailable.value = Boolean(data.history_available)
  handoffHistoryAccessAllowed.value = Boolean(data.history_access_allowed)
  latestHandoffMessageId.value = data.latest_message_id || Math.max(0, ...data.messages.map((item) => item.id))
  seenHandoffMessageIds.value[sessionId] = latestHandoffMessageId.value
}

/**
 * 座席按需加载更早历史。该操作由后端再次校验会话归属并写入审计日志，
 * 避免默认暴露整段 AI 对话。
 */
async function loadEarlierHandoffHistory() {
  const session = selectedHandoff.value
  const earliestMessageId = Math.min(...handoffMessages.value.map((item) => item.id))
  if (!session || !canOperateHandoff.value || !Number.isFinite(earliestMessageId) || handoffHistoryLoading.value) return

  handoffHistoryLoading.value = true
  try {
    const { data } = await staffHandoffApi.history(session.session_id, earliestMessageId)
    // 防止切换会话后旧请求把历史拼接到新会话。
    if (selectedHandoffSessionId.value !== session.session_id) return
    const known = new Set(handoffMessages.value.map((item) => item.id))
    handoffMessages.value = [...data.messages.filter((item) => !known.has(item.id)), ...handoffMessages.value]
    handoffHistoryAvailable.value = data.history_available
    ElMessage.success('已加载更早历史，本次查看已记录审计日志')
  } finally {
    handoffHistoryLoading.value = false
  }
}

async function pollHandoffDetail() {
  const sessionId = selectedHandoffSessionId.value
  if (!sessionId || document.hidden) return
  try {
    const { data } = await staffHandoffApi.detail(sessionId, latestHandoffMessageId.value)
    if (selectedHandoffSessionId.value !== sessionId) return
    const known = new Set(handoffMessages.value.map((item) => item.id))
    handoffMessages.value = [...handoffMessages.value, ...data.messages.filter((item) => !known.has(item.id))]
    latestHandoffMessageId.value = Math.max(latestHandoffMessageId.value, data.latest_message_id || 0)
    seenHandoffMessageIds.value[sessionId] = latestHandoffMessageId.value
    detailPollFailures = 0
  } catch {
    detailPollFailures += 1
  }
}

function scheduleQueuePoll(delay = 3000) {
  if (queuePollTimer !== null) window.clearTimeout(queuePollTimer)
  queuePollTimer = window.setTimeout(async () => {
    if (!document.hidden && !handoffPolling) {
      handoffPolling = true
      try {
        await loadHandoffSessions(true)
        queuePollFailures = 0
      } catch {
        queuePollFailures += 1
      } finally {
        handoffPolling = false
      }
    }
    scheduleQueuePoll(Math.min(15000, 3000 * 2 ** queuePollFailures))
  }, delay)
}

function scheduleDetailPoll(delay = 2000) {
  if (detailPollTimer !== null) window.clearTimeout(detailPollTimer)
  detailPollTimer = window.setTimeout(async () => {
    await pollHandoffDetail()
    scheduleDetailPoll(Math.min(15000, 2000 * 2 ** detailPollFailures))
  }, delay)
}

function startStaffPolling() {
  if (queuePollTimer !== null) return
  scheduleQueuePoll()
  scheduleDetailPoll()
  heartbeatTimer = window.setInterval(() => {
    if (!document.hidden) void staffHandoffApi.heartbeat().catch(() => undefined)
  }, 10000)
}

function stopStaffPolling() {
  if (queuePollTimer !== null) window.clearInterval(queuePollTimer)
  if (detailPollTimer !== null) window.clearInterval(detailPollTimer)
  if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer)
  queuePollTimer = detailPollTimer = heartbeatTimer = null
}

function handleStaffVisibility() {
  if (!document.hidden) {
    void staffHandoffApi.heartbeat().catch(() => undefined)
    scheduleQueuePoll(0)
    scheduleDetailPoll(0)
  }
}

function selectTicket(ticket: Ticket) {
  selectedTicketNo.value = ticket.ticketNo
  draftMessage.value = ''
}

async function selectHandoff(session: StaffHandoffSession) {
  selectedHandoffSessionId.value = session.session_id
  handoffReply.value = ''
  await loadHandoffDetail(session.session_id)
}

async function claimTicket() {
  if (!selectedTicket.value || !auth.user) return
  acting.value = true
  try {
    const { data } = await staffTicketApi.assign(selectedTicket.value.ticketNo, selectedTicket.value.assignedGroup || '客服组')
    ElMessage.success(`已领取工单，当前状态：${data.status}`)
    queueScope.value = 'mine'
    await loadTickets()
  } finally {
    acting.value = false
  }
}

async function startTicket() {
  if (!selectedTicket.value || !auth.user) return
  acting.value = true
  try {
    const { data } = await staffTicketApi.start(selectedTicket.value.ticketNo)
    ElMessage.success(`已开始处理，当前状态：${data.status}`)
    await loadTickets()
  } finally {
    acting.value = false
  }
}

async function closeTicket() {
  if (!selectedTicket.value || !auth.user) return
  acting.value = true
  try {
    const { data } = await staffTicketApi.close(selectedTicket.value.ticketNo, closeReason.value)
    ElMessage.success(`已关闭工单，当前状态：${data.status}`)
    await loadTickets()
  } finally {
    acting.value = false
  }
}

async function generateDraft() {
  if (!selectedTicket.value || generatingDraft.value) return
  generatingDraft.value = true
  try {
    const { data } = await staffReplyApi.draft(selectedTicket.value.ticketNo, closeReason.value)
    draftMessage.value = data.draft_message
    // 模型暂不可用时后端会安全回退，明确提示坐席仍需按真实处理结果审核后发送。
    ElMessage.success(data.generation_mode === 'llm' ? '已根据工单与会话生成 AI 话术草稿' : '已生成安全话术草稿，请结合处理结果确认')
  } finally {
    generatingDraft.value = false
  }
}

async function sendReply() {
  if (!selectedTicket.value || !draftMessage.value.trim()) {
    ElMessage.warning('请先填写客户可见内容')
    return
  }
  await staffReplyApi.send(selectedTicket.value.ticketNo, draftMessage.value)
  draftMessage.value = ''
  ElMessage.success('已发送给客户')
}

async function acceptHandoff() {
  if (!selectedHandoff.value) return
  acting.value = true
  try {
    await staffHandoffApi.accept(selectedHandoff.value.session_id)
    ElMessage.success('已接入人工会话')
    await loadHandoffSessions(true)
  } finally {
    acting.value = false
  }
}

async function sendHandoffReply() {
  if (!selectedHandoff.value || !handoffReply.value.trim()) {
    ElMessage.warning('请先填写人工回复内容')
    return
  }
  acting.value = true
  try {
    await staffHandoffApi.reply(selectedHandoff.value.session_id, handoffReply.value)
    handoffReply.value = ''
    ElMessage.success('已发送给客户')
    await loadHandoffDetail(selectedHandoff.value.session_id)
  } finally {
    acting.value = false
  }
}

async function closeHandoff() {
  if (!selectedHandoff.value) return
  acting.value = true
  try {
    await staffHandoffApi.close(selectedHandoff.value.session_id, handoffCloseMessage.value)
    ElMessage.success('已结束人工接管，后续回到智能助手协助')
    await loadHandoffSessions(false)
  } finally {
    acting.value = false
  }
}

async function createHandoffTicket() {
  if (!selectedHandoff.value) return
  acting.value = true
  try {
    const { data } = await staffHandoffApi.createTicket(selectedHandoff.value.session_id)
    ElMessage.success(`跟进工单已就绪：${data.ticket.ticketNo}`)
  } finally {
    acting.value = false
  }
}

async function logout() {
  stopStaffPolling()
  await staffHandoffApi.leave().catch(() => undefined)
  auth.logout()
  await router.push('/staff/login')
}

onMounted(async () => {
  await staffHandoffApi.heartbeat().catch(() => undefined)
  await Promise.all([loadTickets(), loadHandoffSessions()])
  document.addEventListener('visibilitychange', handleStaffVisibility)
  startStaffPolling()
})
onBeforeUnmount(() => {
  stopStaffPolling()
  document.removeEventListener('visibilitychange', handleStaffVisibility)
  void staffHandoffApi.leave().catch(() => undefined)
})
</script>

<template>
  <el-container class="app-shell">
    <el-aside class="sidebar" width="360px">
      <div class="sidebar-header">
        <h2>我的工单</h2>
        <div class="header-actions">
          <el-button plain @click="router.push('/staff/rag-evaluation')">RAG 评测</el-button>
          <el-button plain @click="router.push('/staff/system-monitor')">系统监控</el-button>
          <el-button :icon="SwitchButton" plain @click="logout">退出</el-button>
        </div>
      </div>
      <p class="muted">当前坐席：{{ auth.user?.display_name }}</p>

      <el-segmented
        v-model="workMode"
        :options="[
          { label: '工单', value: 'tickets' },
          { label: `人工会话 ${handoffSessions.length}`, value: 'handoff' }
        ]"
        class="full-button"
      />

      <template v-if="workMode === 'tickets'">
        <el-segmented
          v-model="queueScope"
          :options="[
            { label: '我的', value: 'mine' },
            { label: '可领取', value: 'claimable' }
          ]"
          class="full-button"
        />
        <el-select v-model="selectedStatuses" class="full-button" multiple collapse-tags collapse-tags-tooltip>
          <el-option v-for="status in statusOptions" :key="status" :label="status" :value="status" />
        </el-select>
        <el-button :icon="Refresh" :loading="loading" class="full-button" @click="loadTickets">刷新工单</el-button>
      </template>
      <template v-else>
        <el-segmented
          v-model="handoffScope"
          :options="[{ label: '待接入', value: 'pending' }, { label: '我的会话', value: 'mine' }]"
          class="full-button"
        />
        <el-button :icon="Refresh" :loading="handoffLoading" class="full-button" @click="loadHandoffSessions(false)">
          刷新人工会话
        </el-button>
      </template>

      <div v-if="workMode === 'tickets'" class="ticket-list">
        <button
          v-for="ticket in visibleTickets"
          :key="ticket.ticketNo"
          :class="['ticket-item', { active: ticket.ticketNo === selectedTicketNo }]"
          @click="selectTicket(ticket)"
        >
          <strong>{{ ticket.ticketNo }}</strong>
          <span>{{ ticket.title }}</span>
          <div>
            <el-tag :type="statusType(ticket.status)" size="small">{{ ticket.status }}</el-tag>
            <el-tag size="small" type="warning">{{ ticket.priority || 'medium' }}</el-tag>
          </div>
          <small>处理人：{{ ticket.handlerId || '未分配' }}</small>
          <small v-if="ticket.urgeCount">催办：{{ ticket.urgeCount }} 次</small>
        </button>
        <el-empty v-if="!visibleTickets.length && !loading" description="当前队列没有工单" />
      </div>
      <div v-else class="ticket-list">
        <button
          v-for="session in visibleHandoffs"
          :key="session.session_id"
          :class="['ticket-item', { active: session.session_id === selectedHandoffSessionId }]"
          @click="selectHandoff(session)"
        >
          <strong>{{ session.title || session.session_id }}</strong>
          <span>客户：{{ session.customer_id }}</span>
          <div>
            <el-tag :type="session.handoff_status === 'ACTIVE' ? 'success' : 'warning'" size="small">
              {{ session.handoff_status === 'ACTIVE' ? '已接入' : '待接入' }}
            </el-tag>
            <el-tag v-if="session.handoff_reason" size="small" type="info">{{ session.handoff_reason }}</el-tag>
            <el-tag v-if="(session.latest_message_id || 0) > (seenHandoffMessageIds[session.session_id] || 0)" size="small" type="danger">新消息</el-tag>
          </div>
          <small>接入人：{{ session.human_assigned_staff_name || '未接入' }}</small>
          <small v-if="session.linked_ticket_no">跟进工单：{{ session.linked_ticket_no }}</small>
          <small>更新时间：{{ session.updated_at || '-' }}</small>
        </button>
        <el-empty v-if="!handoffSessions.length && !handoffLoading" description="当前没有待接入人工会话" />
      </div>
    </el-aside>

    <el-main class="main-panel">
      <header class="page-header">
        <div>
          <h1>客服坐席工作台</h1>
          <p>处理自己名下工单，填写处理结果，并确认客户可见回复</p>
        </div>
      </header>

      <template v-if="workMode === 'handoff'">
        <el-empty v-if="!selectedHandoff" description="请选择人工会话" />
        <section v-else class="staff-grid handoff-grid">
          <el-card shadow="never">
            <template #header>
              <div class="card-header">
                <span>人工会话</span>
                <el-tag :type="selectedHandoff.handoff_status === 'ACTIVE' ? 'success' : 'warning'">
                  {{ selectedHandoff.handoff_status === 'ACTIVE' ? '人工接管中' : '等待接入' }}
                </el-tag>
              </div>
            </template>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="会话编号">{{ selectedHandoff.session_id }}</el-descriptions-item>
              <el-descriptions-item label="交接原因">{{ handoffSummary?.handoff_reason || selectedHandoff.handoff_reason || '-' }}</el-descriptions-item>
              <el-descriptions-item label="请求时间">{{ selectedHandoff.human_requested_at || '-' }}</el-descriptions-item>
              <el-descriptions-item label="接入坐席">{{ selectedHandoff.human_assigned_staff_name || '-' }}</el-descriptions-item>
              <el-descriptions-item label="当前意图">{{ handoffSummary?.intent || selectedHandoff.intent || '-' }}</el-descriptions-item>
              <el-descriptions-item label="关联工单">{{ handoffSummary?.linked_ticket_no || selectedHandoff.linked_ticket_no || '-' }}</el-descriptions-item>
            </el-descriptions>

            <section class="handoff-summary-card">
              <div class="handoff-summary-header">
                <strong>交接摘要</strong>
                <el-tag size="small" type="info">最小必要信息</el-tag>
              </div>
              <p>{{ handoffSummary?.ai_summary || selectedHandoff.ai_summary || selectedHandoff.title || '暂无可展示的交接摘要。' }}</p>
            </section>

            <div class="handoff-history-toolbar">
              <span>默认仅展示最近相关对话</span>
              <el-button
                v-if="handoffHistoryAvailable && handoffHistoryAccessAllowed"
                :loading="handoffHistoryLoading"
                link
                type="primary"
                @click="loadEarlierHandoffHistory"
              >
                展开更早历史
              </el-button>
              <span v-else-if="selectedHandoff.handoff_status === 'PENDING'" class="handoff-history-hint">
                接入会话后可查看最近相关对话
              </span>
            </div>

            <el-empty
              v-if="!handoffMessages.length"
              :description="selectedHandoff.handoff_status === 'PENDING' ? '请先接入会话，系统将提供最近相关对话。' : '暂无可展示的会话消息。'"
              :image-size="72"
            />
            <div class="handoff-messages">
              <div
                v-for="message in handoffMessages"
                :key="message.id"
                :class="['handoff-message', message.sender_type]"
              >
                <small>{{ message.sender_type }} · {{ message.created_at }}</small>
                <p>{{ message.content }}</p>
              </div>
            </div>
          </el-card>

          <el-card shadow="never">
            <template #header>人工接管操作</template>
            <el-alert
              v-if="selectedHandoff.handoff_status === 'PENDING'"
              title="该客户正在等待人工接入，接入后智能助手会暂停自动回复。"
              :closable="false"
              type="warning"
              show-icon
            />
            <el-alert
              v-else-if="!canOperateHandoff"
              title="该会话已由其他坐席接入，当前账号不能回复。"
              :closable="false"
              type="info"
              show-icon
            />
            <div class="action-row section-gap">
              <el-button
                v-if="selectedHandoff.handoff_status === 'PENDING'"
                :loading="acting"
                type="primary"
                @click="acceptHandoff"
              >
                接入会话
              </el-button>
            </div>
            <el-form label-position="top">
              <el-form-item label="人工回复">
                <el-input
                  v-model="handoffReply"
                  :disabled="!canOperateHandoff"
                  :rows="8"
                  placeholder="请输入要直接发送给客户的人工回复。"
                  type="textarea"
                />
              </el-form-item>
              <el-button :disabled="!canOperateHandoff" :loading="acting" type="primary" @click="sendHandoffReply">
                发送给客户
              </el-button>
              <el-button :disabled="!canOperateHandoff" :loading="acting" @click="createHandoffTicket">
                创建跟进工单
              </el-button>

              <el-divider />
              <el-form-item label="结束说明">
                <el-input v-model="handoffCloseMessage" :disabled="!canOperateHandoff" :rows="3" type="textarea" />
              </el-form-item>
              <el-button :disabled="!canOperateHandoff" :loading="acting" @click="closeHandoff">
                结束人工接管
              </el-button>
            </el-form>
          </el-card>
        </section>
      </template>

      <el-empty v-else-if="!selectedTicket" description="请选择工单" />
      <template v-else>
        <section class="staff-grid">
          <el-card shadow="never">
            <template #header>工单详情</template>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="工单号">{{ selectedTicket.ticketNo }}</el-descriptions-item>
              <el-descriptions-item label="状态">
                <el-tag :type="statusType(selectedTicket.status)">{{ selectedTicket.status }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="优先级">{{ selectedTicket.priority || '-' }}</el-descriptions-item>
              <el-descriptions-item label="处理组">{{ selectedTicket.assignedGroup || '-' }}</el-descriptions-item>
              <el-descriptions-item label="处理人">{{ selectedTicket.handlerId || '-' }}</el-descriptions-item>
              <el-descriptions-item label="派单方式">{{ selectedTicket.assignedBy || '-' }}</el-descriptions-item>
              <el-descriptions-item label="关联订单">{{ selectedTicket.orderNo || '-' }}</el-descriptions-item>
              <el-descriptions-item label="SLA">{{ selectedTicket.slaDeadline || '-' }}</el-descriptions-item>
              <el-descriptions-item label="催办次数">{{ selectedTicket.urgeCount || 0 }}</el-descriptions-item>
              <el-descriptions-item label="最近催办">{{ selectedTicket.lastUrgedAt || '-' }}</el-descriptions-item>
            </el-descriptions>

            <el-alert
              v-if="selectedTicket.lastUrgeReason"
              :title="`客户催办：${selectedTicket.lastUrgeReason}`"
              :closable="false"
              class="section-gap"
              type="warning"
              show-icon
            />

            <h3>客户问题</h3>
            <el-alert :title="selectedTicket.content || selectedTicket.title" :closable="false" type="info" />

            <h3>AI 摘要</h3>
            <p class="plain-text">{{ selectedTicket.aiSummary || '暂无 AI 摘要' }}</p>
          </el-card>

          <el-card shadow="never">
            <template #header>处理操作</template>
            <el-form label-position="top">
              <el-alert
                v-if="!canOperate && !canClaim"
                title="该工单不在你名下，不能处理。"
                :closable="false"
                type="warning"
              />
              <div class="action-row">
                <el-button v-if="canClaim" :icon="UserFilled" :loading="acting" type="primary" @click="claimTicket">
                  领取工单
                </el-button>
                <el-button :disabled="!canOperate" :icon="Check" :loading="acting" @click="startTicket">开始处理</el-button>
                <el-button :disabled="!canOperate" :icon="Close" :loading="acting" type="danger" @click="closeTicket">
                  关闭工单
                </el-button>
              </div>

              <el-divider />
              <el-form-item label="处理结果">
                <el-input v-model="closeReason" :disabled="!canOperate" :rows="3" type="textarea" />
              </el-form-item>
              <el-button :disabled="!canOperate || generatingDraft" :loading="generatingDraft" @click="generateDraft">
                {{ generatingDraft ? '正在基于工单事实生成…' : '生成客户话术草稿' }}
              </el-button>
              <p v-if="generatingDraft" class="draft-generating-hint">正在结合工单处理结果、客户诉求和最近会话生成草稿，通常几秒内完成。</p>
              <el-form-item class="reply-editor" label="客户可见内容">
                <el-input
                  v-model="draftMessage"
                  :disabled="!canOperate || generatingDraft"
                  :rows="8"
                  placeholder="请先生成草稿，或直接填写要发送给客户的处理说明。"
                  type="textarea"
                />
              </el-form-item>
              <el-button :disabled="!canOperate || generatingDraft" type="primary" @click="sendReply">确认发送给客户</el-button>
            </el-form>
          </el-card>
        </section>
      </template>
    </el-main>
  </el-container>
</template>
