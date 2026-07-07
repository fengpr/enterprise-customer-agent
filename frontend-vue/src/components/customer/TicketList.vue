<script setup lang="ts">
import { Loading } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'
import type { Ticket } from '@/types/api'
import StatusTag from './StatusTag.vue'

const props = defineProps<{
  tickets: Ticket[]
  selectedTicketNo: string | null
  loading?: boolean
  collapsed?: boolean
}>()

const emit = defineEmits<{
  select: [ticketNo: string]
  toggleCollapse: []
}>()

const activeTab = ref('全部工单')
const tabs = ['全部工单', '待处理', '处理中', '待补充', '已完成']

const filteredTickets = computed(() => {
  if (activeTab.value === '全部工单') return props.tickets
  if (activeTab.value === '待处理') return props.tickets.filter((ticket) => ticket.status === 'PENDING_ASSIGN')
  if (activeTab.value === '处理中') return props.tickets.filter((ticket) => ['PENDING_PROCESS', 'PROCESSING'].includes(String(ticket.status)))
  if (activeTab.value === '待补充') return props.tickets.filter((ticket) => ['REOPENED', 'TRANSFERRED', 'PENDING_SUPPLEMENT'].includes(String(ticket.status)))
  return props.tickets.filter((ticket) => ticket.status === 'CLOSED')
})

function shortTime(value?: string | null) {
  return value ? value.replace('T', ' ').slice(5, 16) : '-'
}

function ticketTypeLabel(type?: string | null) {
  const labels: Record<string, string> = {
    refund: '退款/退货',
    exchange: '换货服务',
    complaint: '投诉反馈',
    repair: '维修服务',
    logistics: '物流咨询',
    invoice: '发票问题',
    member: '会员服务',
    other: '综合咨询'
  }
  return labels[String(type || '').toLowerCase()] || '服务工单'
}

function shortTicketNo(ticketNo?: string | null) {
  if (!ticketNo) return '-'
  return ticketNo.length > 14 ? `${ticketNo.slice(0, 6)}...${ticketNo.slice(-6)}` : ticketNo
}
</script>

<template>
  <section :class="['tickets-card', 'dashboard-card', { 'is-collapsed': collapsed }]">
    <h2>我的工单</h2>
    <div class="module-title-actions tickets-title-actions">
      <span v-if="!collapsed && loading && tickets.length" class="ticket-refresh-state" aria-live="polite">
        <el-icon class="is-loading"><Loading /></el-icon>
        更新中
      </span>
      <el-tooltip :content="collapsed ? '展开工单列表' : '收起工单列表'" placement="top">
        <el-button class="collapse-button" text @click="emit('toggleCollapse')">
          {{ collapsed ? '+' : '-' }}
        </el-button>
      </el-tooltip>
    </div>
    <div v-if="!collapsed" class="ticket-tabs">
      <button v-for="tab in tabs" :key="tab" :class="{ active: tab === activeTab }" @click="activeTab = tab">
        {{ tab }}
      </button>
    </div>
    <el-skeleton v-if="!collapsed && loading && !tickets.length" :rows="5" animated />
    <div v-else-if="!collapsed" class="ticket-table">
      <div class="ticket-head">
        <span>工单号</span>
        <span>类型</span>
        <span>当前状态</span>
        <span>更新时间</span>
      </div>
      <button
        v-for="ticket in filteredTickets"
        :key="ticket.ticketNo"
        :class="['ticket-row-modern', { active: ticket.ticketNo === selectedTicketNo }]"
        @click="emit('select', ticket.ticketNo)"
      >
        <span class="ticket-no-cell" :title="ticket.ticketNo">{{ shortTicketNo(ticket.ticketNo) }}</span>
        <span>{{ ticketTypeLabel(ticket.ticketType) }}</span>
        <StatusTag :status="ticket.status" />
        <span>{{ shortTime(ticket.updatedAt) }}</span>
      </button>
      <el-empty v-if="!filteredTickets.length" description="暂无工单" />
    </div>
  </section>
</template>
