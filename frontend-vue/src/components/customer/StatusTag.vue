<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  status?: string | null
}>()

const label = computed(() => {
  const mapping: Record<string, string> = {
    SIGNED: '已签收',
    SHIPPED: '已发货',
    COMPLETED: '已完成',
    PENDING_ASSIGN: '待分派',
    PENDING_PROCESS: '处理中',
    PROCESSING: '处理中',
    REOPENED: '待处理',
    TRANSFERRED: '待处理',
    CLOSED: '已完成',
    PENDING_SUPPLEMENT: '待补充'
  }
  return mapping[String(props.status || '')] || props.status || '未知'
})

const tagType = computed(() => {
  if (['CLOSED', 'COMPLETED', 'SIGNED'].includes(String(props.status))) return 'success'
  if (['PENDING_PROCESS', 'PROCESSING'].includes(String(props.status))) return 'warning'
  if (['PENDING_ASSIGN', 'SHIPPED'].includes(String(props.status))) return 'primary'
  if (['REOPENED', 'TRANSFERRED', 'PENDING_SUPPLEMENT'].includes(String(props.status))) return 'danger'
  return 'info'
})
</script>

<template>
  <el-tag :type="tagType" size="small" effect="light">{{ label }}</el-tag>
</template>
