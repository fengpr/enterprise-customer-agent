<script setup lang="ts">
import { Document, View } from '@element-plus/icons-vue'
import { computed } from 'vue'
import type { Ticket } from '@/types/api'
import StatusTag from './StatusTag.vue'

const props = defineProps<{
  ticket: Ticket | null
  collapsed?: boolean
}>()

const emit = defineEmits<{
  toggleCollapse: []
}>()

const steps = [
  { title: '已提交', desc: '您已提交服务申请', time: '已记录' },
  { title: '待分派', desc: '系统等待工作人员分派处理', time: '待确认' },
  { title: '处理中', desc: '客服正在审核您的申请', time: '当前步骤' },
  { title: '已完成', desc: '处理完成，等待您确认', time: '待完成' }
]

function activeIndex(status?: string) {
  if (status === 'CLOSED') return 3
  if (['PENDING_PROCESS', 'PROCESSING'].includes(String(status))) return 2
  if (status === 'PENDING_ASSIGN') return 1
  return 0
}

function ticketTypeLabel(type?: string | null) {
  const labels: Record<string, string> = {
    refund: '退款/退货申请',
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

function statusExplanation(status?: string | null) {
  if (status === 'PENDING_ASSIGN') return '您的申请已提交，正在等待工作人员分派处理。'
  if (['PENDING_PROCESS', 'PROCESSING'].includes(String(status))) return '客服正在审核您的申请，如需补充信息会继续联系您。'
  if (status === 'CLOSED') return '该工单已处理完成，如仍有疑问可以重新发起咨询。'
  return '我们已收到您的问题，会根据工单状态继续推进。'
}

function nextStepText(status?: string | null) {
  if (status === 'PENDING_ASSIGN') return '下一步：工作人员确认后会分派给合适的客服处理。'
  if (['PENDING_PROCESS', 'PROCESSING'].includes(String(status))) return '下一步：客服会结合订单和业务规则继续审核，并同步处理结果。'
  if (status === 'CLOSED') return '下一步：请确认处理结果，如未解决可再次联系在线客服。'
  return '下一步：请保持关注工单状态更新。'
}

function customerIssue(ticket: Ticket) {
  const text = ticket.title || ticket.content || ticket.aiSummary || ''
  return text
    .replace(/客户诉求[:：]\s*/g, '')
    .replace(/业务动作[:：][^\n]+/g, '')
    .replace(/用户目的[:：][^\n]+/g, '')
    .replace(/已收集信息[:：]/g, '')
    .replace(/-\s*order_no[:：]\s*[A-Z0-9-]+\s*/gi, '')
    .replace(/-\s*after_sale_reason[:：]\s*/gi, '原因：')
    .replace(/-\s*description[:：]\s*/gi, '说明：')
    .replace(/订单上下文[:：][\s\S]*/g, '')
    .replace(/\s+/g, ' ')
    .trim() || '客户服务申请'
}

const detail = computed(() => {
  if (!props.ticket) return null
  return {
    typeLabel: ticketTypeLabel(props.ticket.ticketType),
    issue: customerIssue(props.ticket),
    statusText: statusExplanation(props.ticket.status),
    nextStep: nextStepText(props.ticket.status)
  }
})
</script>

<template>
  <aside :class="['progress-panel', 'dashboard-card', { 'is-collapsed': collapsed }]">
    <div class="module-title progress-title">
      <div class="module-title-actions">
        <el-tooltip :content="collapsed ? '展开工单进度' : '收起工单进度'" placement="top">
          <el-button class="collapse-button" text @click="emit('toggleCollapse')">
            {{ collapsed ? '+' : '-' }}
          </el-button>
        </el-tooltip>
      </div>
    </div>
    <template v-if="!collapsed && ticket">
      <h2>工单进度</h2>
      <div class="progress-summary">
        <div>
          <strong>{{ ticket.ticketNo }}</strong>
          <p>{{ detail?.typeLabel }}</p>
        </div>
        <StatusTag :status="ticket.status" />
      </div>
      <p class="detail-line">关联订单：{{ ticket.orderNo || '-' }}</p>
      <p class="detail-line">更新时间：{{ ticket.updatedAt?.replace('T', ' ').slice(0, 16) || '-' }}</p>

      <div class="customer-next-card">
        <strong>当前状态说明</strong>
        <p>{{ detail?.statusText }}</p>
        <span>{{ detail?.nextStep }}</span>
      </div>

      <div class="progress-timeline">
        <div
          v-for="(step, index) in steps"
          :key="step.title"
          :class="['timeline-step', { done: index <= activeIndex(ticket.status), current: index === activeIndex(ticket.status) }]"
        >
          <span class="timeline-dot">{{ index < activeIndex(ticket.status) ? '✓' : '' }}</span>
          <div>
            <strong>{{ step.title }}</strong>
            <p>{{ step.desc }}</p>
            <time>{{ index === activeIndex(ticket.status) ? '当前步骤' : step.time }}</time>
          </div>
        </div>
      </div>

      <div class="ticket-detail-block">
        <h3>工单详情</h3>
        <p><span>问题描述</span>{{ detail?.issue }}</p>
        <p><span>期望解决</span>{{ ticket.lastUrgeReason || '请工作人员尽快处理并同步进展。' }}</p>
        <p><span>催办次数</span>{{ ticket.urgeCount || 0 }} 次</p>
        <p class="attachment-line"><span>附件</span><em><el-icon><Document /></el-icon> 暂无客户上传附件</em></p>
      </div>

      <el-button :icon="View" class="full-button" type="primary" plain>查看工单详情</el-button>
    </template>
    <el-empty v-else description="请选择工单查看进度" />
  </aside>
</template>
