<script setup lang="ts">
import { Bell } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { customerApi } from '@/api/customer'
import type { Ticket, TicketResult } from '@/types/api'
import { statusType, ticketFromResult } from '@/utils/ticket'

const props = defineProps<{
  ticketResult?: TicketResult
}>()

const latestTicket = ref<Ticket | null>(null)
const urging = ref(false)

const fallbackTicket = computed(() => ticketFromResult(props.ticketResult))
const displayTicket = computed(() => latestTicket.value ?? fallbackTicket.value)

async function refreshTicket() {
  const ticketNo = fallbackTicket.value?.ticketNo
  if (!ticketNo) {
    return
  }
  try {
    const { data } = await customerApi.ticket(ticketNo)
    latestTicket.value = data
  } catch {
    latestTicket.value = null
  }
}

async function urgeTicket() {
  const ticketNo = displayTicket.value?.ticketNo
  if (!ticketNo) {
    return
  }
  urging.value = true
  try {
    const { data } = await customerApi.urgeTicket(ticketNo, '客户在自助入口点击催办进度')
    latestTicket.value = data
    ElMessage.success('已帮您催办，工作人员会在原工单中继续跟进')
  } finally {
    urging.value = false
  }
}

watch(() => fallbackTicket.value?.ticketNo, refreshTicket)
onMounted(refreshTicket)
</script>

<template>
  <el-alert v-if="ticketResult?.status === 'failed'" show-icon title="问题已记录，客服会继续跟进处理。" type="info" />
  <div v-else-if="displayTicket" class="ticket-line">
    <span>工单号：{{ displayTicket.ticketNo }}</span>
    <el-tag :type="statusType(displayTicket.status)">
      {{ displayTicket.status || '待客服处理' }}
    </el-tag>
    <el-tag v-if="displayTicket.urgeCount" type="warning">已催办 {{ displayTicket.urgeCount }} 次</el-tag>
    <el-button
      v-if="displayTicket.status !== 'CLOSED'"
      :icon="Bell"
      :loading="urging"
      size="small"
      text
      type="primary"
      @click="urgeTicket"
    >
      催一下进度
    </el-button>
  </div>
</template>
