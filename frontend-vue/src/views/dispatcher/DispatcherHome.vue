<script setup lang="ts">
import { Cpu, Refresh, Setting, SwitchButton, UserFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { staffMemberApi, staffTicketApi } from '@/api/staff'
import { useAuthStore } from '@/stores/auth'
import type { StaffMemberStatus, Ticket } from '@/types/api'
import { statusType } from '@/utils/ticket'

const router = useRouter()
const auth = useAuthStore()
const tickets = ref<Ticket[]>([])
const staffMembers = ref<StaffMemberStatus[]>([])
const selectedTicketNo = ref<string | null>(null)
const selectedStaffId = ref<number | null>(null)
const selectedStatuses = ref(['PENDING_ASSIGN', 'TRANSFERRED', 'REOPENED'])
const loading = ref(false)
const acting = ref(false)
const configDialogVisible = ref(false)
const configSaving = ref(false)
const editingStaff = ref<StaffMemberStatus | null>(null)
const configForm = ref({
  online: true,
  acceptingTickets: true,
  maxActiveTickets: 1
})

const statusOptions = ['PENDING_ASSIGN', 'PENDING_PROCESS', 'PROCESSING', 'TRANSFERRED', 'REOPENED', 'CLOSED']
const selectedTicket = computed(() => tickets.value.find((ticket) => ticket.ticketNo === selectedTicketNo.value) ?? null)
const selectedStaff = computed(() => staffMembers.value.find((staff) => staff.userId === selectedStaffId.value) ?? null)
const assignableStaff = computed(() =>
  staffMembers.value.filter((staff) => staff.online && staff.acceptingTickets && staff.activeTickets < staff.maxActiveTickets)
)
const urgentTickets = computed(() =>
  tickets.value.filter((ticket) => {
    if (!ticket.slaDeadline) return false
    return new Date(ticket.slaDeadline).getTime() - Date.now() < 4 * 60 * 60 * 1000
  })
)

async function loadDashboard() {
  loading.value = true
  try {
    const [{ data: ticketData }, { data: memberData }] = await Promise.all([
      staffTicketApi.list(selectedStatuses.value.join(',')),
      staffMemberApi.list()
    ])
    tickets.value = ticketData
    staffMembers.value = memberData
    if (!selectedTicketNo.value || !ticketData.some((ticket) => ticket.ticketNo === selectedTicketNo.value)) {
      selectedTicketNo.value = ticketData[0]?.ticketNo ?? null
    }
    if (!selectedStaffId.value) {
      selectedStaffId.value = assignableStaff.value[0]?.userId ?? memberData[0]?.userId ?? null
    }
  } finally {
    loading.value = false
  }
}

function selectTicket(ticket: Ticket) {
  selectedTicketNo.value = ticket.ticketNo
  if (ticket.handlerId) {
    selectedStaffId.value = ticket.handlerId
  }
}

function selectStaff(staff: StaffMemberStatus) {
  selectedStaffId.value = staff.userId
}

function openStaffConfig(staff: StaffMemberStatus) {
  editingStaff.value = staff
  selectedStaffId.value = staff.userId
  configForm.value = {
    online: staff.online,
    acceptingTickets: staff.acceptingTickets,
    maxActiveTickets: staff.maxActiveTickets
  }
  configDialogVisible.value = true
}

async function saveStaffConfig() {
  if (!editingStaff.value) return
  configSaving.value = true
  try {
    await staffMemberApi.updateAvailability(editingStaff.value.userId, configForm.value)
    ElMessage.success('坐席配置已保存')
    configDialogVisible.value = false
    await loadDashboard()
  } finally {
    configSaving.value = false
  }
}

async function autoAssignTicket() {
  if (!selectedTicket.value) return
  acting.value = true
  try {
    const { data } = await staffTicketApi.autoAssign(selectedTicket.value.ticketNo)
    ElMessage.success(data.handlerId ? `智能派单完成：${data.handlerId}` : '暂无可用坐席，已记录派单原因')
    await loadDashboard()
  } finally {
    acting.value = false
  }
}

async function manualAssignTicket() {
  if (!selectedTicket.value || !selectedStaff.value) {
    ElMessage.warning('请选择工单和目标坐席')
    return
  }
  acting.value = true
  try {
    const { data } = await staffTicketApi.assign(
      selectedTicket.value.ticketNo,
      selectedStaff.value.userId,
      selectedStaff.value.groupName
    )
    ElMessage.success(`已手动分派给 ${selectedStaff.value.displayName}，当前状态：${data.status}`)
    await loadDashboard()
  } finally {
    acting.value = false
  }
}

async function logout() {
  auth.logout()
  await router.push('/dispatcher/login')
}

onMounted(loadDashboard)
</script>

<template>
  <el-container class="app-shell">
    <el-aside class="sidebar" width="360px">
      <div class="sidebar-header">
        <h2>调度队列</h2>
        <el-button :icon="SwitchButton" plain @click="logout">退出</el-button>
      </div>
      <p class="muted">当前调度：{{ auth.user?.display_name }}</p>

      <el-select v-model="selectedStatuses" class="full-button" multiple collapse-tags collapse-tags-tooltip>
        <el-option v-for="status in statusOptions" :key="status" :label="status" :value="status" />
      </el-select>
      <el-button :icon="Refresh" :loading="loading" class="full-button" @click="loadDashboard">刷新调度台</el-button>

      <el-alert
        v-if="urgentTickets.length"
        :title="`有 ${urgentTickets.length} 个工单接近 SLA`"
        class="section-gap"
        type="warning"
        show-icon
      />

      <div class="ticket-list">
        <button
          v-for="ticket in tickets"
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
        <el-empty v-if="!tickets.length && !loading" description="当前没有需要调度的工单" />
      </div>
    </el-aside>

    <el-main class="main-panel">
      <header class="page-header">
        <div>
          <h1>工单调度台</h1>
          <p>查看派单建议、坐席负载和客户反馈，并对工单进行人工分派或改派</p>
        </div>
      </header>

      <section class="dispatcher-grid">
        <el-card shadow="never">
          <template #header>工单详情与派单建议</template>
          <el-empty v-if="!selectedTicket" description="请选择工单" />
          <template v-else>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="工单号">{{ selectedTicket.ticketNo }}</el-descriptions-item>
              <el-descriptions-item label="状态">
                <el-tag :type="statusType(selectedTicket.status)">{{ selectedTicket.status }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="客户">{{ selectedTicket.customerId || '-' }}</el-descriptions-item>
              <el-descriptions-item label="优先级">{{ selectedTicket.priority || '-' }}</el-descriptions-item>
              <el-descriptions-item label="处理组">{{ selectedTicket.assignedGroup || '-' }}</el-descriptions-item>
              <el-descriptions-item label="处理人">{{ selectedTicket.handlerId || '-' }}</el-descriptions-item>
              <el-descriptions-item label="派单方式">{{ selectedTicket.assignedBy || '-' }}</el-descriptions-item>
              <el-descriptions-item label="派单分">{{ selectedTicket.assignmentScore ?? '-' }}</el-descriptions-item>
              <el-descriptions-item label="SLA">{{ selectedTicket.slaDeadline || '-' }}</el-descriptions-item>
              <el-descriptions-item label="会话">{{ selectedTicket.externalSessionNo || '-' }}</el-descriptions-item>
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

            <h3>派单建议</h3>
            <el-alert
              :title="selectedTicket.assignmentReason || '暂无建议，可点击智能派单生成推荐坐席和理由。'"
              :closable="false"
              type="success"
            />

            <h3>客户问题</h3>
            <el-alert :title="selectedTicket.content || selectedTicket.title" :closable="false" type="info" />

            <h3>AI 摘要</h3>
            <p class="plain-text">{{ selectedTicket.aiSummary || '暂无 AI 摘要' }}</p>

            <el-divider />
            <el-form label-position="top">
              <el-form-item label="目标客服">
                <el-select v-model="selectedStaffId" filterable placeholder="选择客服人员">
                  <el-option
                    v-for="staff in staffMembers"
                    :key="staff.userId"
                    :disabled="!staff.online || !staff.acceptingTickets || staff.activeTickets >= staff.maxActiveTickets"
                    :label="`${staff.displayName} / ${staff.groupName} / ${staff.activeTickets}/${staff.maxActiveTickets}`"
                    :value="staff.userId"
                  />
                </el-select>
              </el-form-item>
              <div class="action-row">
                <el-button :icon="Cpu" :loading="acting" type="success" @click="autoAssignTicket">智能派单</el-button>
                <el-button :icon="UserFilled" :loading="acting" type="primary" @click="manualAssignTicket">手动分派/改派</el-button>
              </div>
            </el-form>
          </template>
        </el-card>

        <el-card shadow="never">
          <template #header>客服资源状态</template>
          <div class="staff-member-list">
            <div
              v-for="staff in staffMembers"
              :key="staff.userId"
              :class="['staff-member-item', { active: staff.userId === selectedStaffId }]"
              role="button"
              tabindex="0"
              @click="selectStaff(staff)"
            >
              <div>
                <strong>{{ staff.displayName }}</strong>
                <span>{{ staff.groupName }}</span>
              </div>
              <div>
                <el-tag :type="staff.online ? 'success' : 'info'" size="small">
                  {{ staff.online ? '在线' : '离线' }}
                </el-tag>
                <el-tag :type="staff.acceptingTickets ? 'primary' : 'danger'" size="small">
                  {{ staff.acceptingTickets ? '可接单' : '暂停接单' }}
                </el-tag>
                <el-button :icon="Setting" size="small" text @click.stop="openStaffConfig(staff)">配置</el-button>
              </div>
              <el-progress :percentage="Math.min(100, Math.round((staff.activeTickets / staff.maxActiveTickets) * 100))" />
              <small>负载：{{ staff.activeTickets }} / {{ staff.maxActiveTickets }}</small>
              <div class="tag-row">
                <el-tag v-for="skill in staff.skills" :key="skill" size="small">{{ skill }}</el-tag>
              </div>
              <p class="muted">当前工作：{{ staff.currentWork.length ? staff.currentWork.join('；') : '暂无活跃工单' }}</p>
              <p class="muted">客户反馈：{{ staff.recentFeedback.join('；') }}</p>
            </div>
          </div>
        </el-card>
      </section>
    </el-main>
  </el-container>

  <el-dialog v-model="configDialogVisible" title="坐席配置" width="420px">
    <el-form v-if="editingStaff" label-position="top">
      <el-form-item label="坐席">
        <el-input :model-value="`${editingStaff.displayName} / ${editingStaff.groupName}`" disabled />
      </el-form-item>
      <el-form-item label="在线状态">
        <el-switch v-model="configForm.online" active-text="在线" inactive-text="离线" />
      </el-form-item>
      <el-form-item label="接单状态">
        <el-switch v-model="configForm.acceptingTickets" active-text="可接单" inactive-text="暂停接单" />
      </el-form-item>
      <el-form-item label="最大并发量">
        <el-input-number v-model="configForm.maxActiveTickets" :min="1" :max="20" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="configDialogVisible = false">取消</el-button>
      <el-button :loading="configSaving" type="primary" @click="saveStaffConfig">保存</el-button>
    </template>
  </el-dialog>
</template>
