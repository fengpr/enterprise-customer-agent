<script setup lang="ts">
import { Check, Close, Refresh, SwitchButton, UserFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { staffHandoffApi, staffReplyApi, staffTicketApi } from '@/api/staff'
import { useAuthStore } from '@/stores/auth'
import type { ChatMessage, StaffHandoffSession, Ticket } from '@/types/api'
import { statusType } from '@/utils/ticket'

const router = useRouter()
const auth = useAuthStore()
const tickets = ref<Ticket[]>([])
const handoffSessions = ref<StaffHandoffSession[]>([])
const handoffMessages = ref<ChatMessage[]>([])
const selectedTicketNo = ref<string | null>(null)
const selectedHandoffSessionId = ref<string | null>(null)
const loading = ref(false)
const handoffLoading = ref(false)
const acting = ref(false)
const closeReason = ref('问题已处理完成')
const draftMessage = ref('')
const handoffReply = ref('')
const handoffCloseMessage = ref('人工服务已结束，后续可继续由智能助手协助。')
const workMode = ref<'tickets' | 'handoff'>('tickets')
const queueScope = ref<'mine' | 'claimable'>('mine')
const selectedStatuses = ref(['PENDING_ASSIGN', 'PENDING_PROCESS', 'PROCESSING', 'REOPENED'])

const statusOptions = ['PENDING_ASSIGN', 'PENDING_PROCESS', 'PROCESSING', 'REOPENED', 'CLOSED']
const selectedTicket = computed(() => tickets.value.find((item) => item.ticketNo === selectedTicketNo.value) ?? null)
const selectedHandoff = computed(() => handoffSessions.value.find((item) => item.session_id === selectedHandoffSessionId.value) ?? null)
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
  return selectedHandoff.value?.status === 'HUMAN_ACTIVE' && selectedHandoff.value.human_assigned_staff_id === String(auth.user?.user_id)
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
    const { data } = await staffHandoffApi.list()
    handoffSessions.value = data
    if (!keepSelection || !selectedHandoffSessionId.value || !data.some((item) => item.session_id === selectedHandoffSessionId.value)) {
      selectedHandoffSessionId.value = data[0]?.session_id ?? null
    }
    if (selectedHandoffSessionId.value) {
      await loadHandoffDetail(selectedHandoffSessionId.value)
    } else {
      handoffMessages.value = []
    }
  } finally {
    handoffLoading.value = false
  }
}

async function loadHandoffDetail(sessionId: string) {
  const { data } = await staffHandoffApi.detail(sessionId)
  handoffMessages.value = data.messages
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
    const { data } = await staffTicketApi.assign(selectedTicket.value.ticketNo, auth.user.user_id, selectedTicket.value.assignedGroup || '客服组')
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
    const { data } = await staffTicketApi.start(selectedTicket.value.ticketNo, auth.user.user_id)
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
    const { data } = await staffTicketApi.close(selectedTicket.value.ticketNo, auth.user.user_id, closeReason.value)
    ElMessage.success(`已关闭工单，当前状态：${data.status}`)
    await loadTickets()
  } finally {
    acting.value = false
  }
}

async function generateDraft() {
  if (!selectedTicket.value) return
  const { data } = await staffReplyApi.draft(selectedTicket.value.ticketNo, closeReason.value)
  draftMessage.value = data.draft_message
  ElMessage.success('已生成客户话术草稿')
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
    await staffHandoffApi.close(selectedHandoff.value.session_id, handoffCloseMessage.value, 'HUMAN_CLOSED')
    ElMessage.success('已结束人工接管，后续回到智能助手协助')
    await loadHandoffSessions(false)
  } finally {
    acting.value = false
  }
}

async function logout() {
  auth.logout()
  await router.push('/staff/login')
}

onMounted(async () => {
  await Promise.all([loadTickets(), loadHandoffSessions()])
})
</script>

<template>
  <el-container class="app-shell">
    <el-aside class="sidebar" width="360px">
      <div class="sidebar-header">
        <h2>我的工单</h2>
        <div class="header-actions">
          <el-button plain @click="router.push('/staff/rag-evaluation')">RAG 评测</el-button>
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
          v-for="session in handoffSessions"
          :key="session.session_id"
          :class="['ticket-item', { active: session.session_id === selectedHandoffSessionId }]"
          @click="selectHandoff(session)"
        >
          <strong>{{ session.title || session.session_id }}</strong>
          <span>客户：{{ session.customer_id }}</span>
          <div>
            <el-tag :type="session.status === 'HUMAN_ACTIVE' ? 'success' : 'warning'" size="small">
              {{ session.status === 'HUMAN_ACTIVE' ? '已接入' : '待接入' }}
            </el-tag>
            <el-tag v-if="session.handoff_reason" size="small" type="info">{{ session.handoff_reason }}</el-tag>
          </div>
          <small>接入人：{{ session.human_assigned_staff_name || '未接入' }}</small>
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
                <el-tag :type="selectedHandoff.status === 'HUMAN_ACTIVE' ? 'success' : 'warning'">
                  {{ selectedHandoff.status === 'HUMAN_ACTIVE' ? '人工接管中' : '等待接入' }}
                </el-tag>
              </div>
            </template>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="会话编号">{{ selectedHandoff.session_id }}</el-descriptions-item>
              <el-descriptions-item label="客户ID">{{ selectedHandoff.customer_id }}</el-descriptions-item>
              <el-descriptions-item label="请求时间">{{ selectedHandoff.human_requested_at || '-' }}</el-descriptions-item>
              <el-descriptions-item label="接入坐席">{{ selectedHandoff.human_assigned_staff_name || '-' }}</el-descriptions-item>
              <el-descriptions-item label="意图">{{ selectedHandoff.intent || '-' }}</el-descriptions-item>
              <el-descriptions-item label="摘要">{{ selectedHandoff.ai_summary || selectedHandoff.title || '-' }}</el-descriptions-item>
            </el-descriptions>

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
              v-if="selectedHandoff.status === 'HUMAN_PENDING'"
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
                v-if="selectedHandoff.status === 'HUMAN_PENDING'"
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
              <el-button :disabled="!canOperate" @click="generateDraft">生成客户话术草稿</el-button>
              <el-form-item class="reply-editor" label="客户可见内容">
                <el-input
                  v-model="draftMessage"
                  :disabled="!canOperate"
                  :rows="8"
                  placeholder="请先生成草稿，或直接填写要发送给客户的处理说明。"
                  type="textarea"
                />
              </el-form-item>
              <el-button :disabled="!canOperate" type="primary" @click="sendReply">确认发送给客户</el-button>
            </el-form>
          </el-card>
        </section>
      </template>
    </el-main>
  </el-container>
</template>
