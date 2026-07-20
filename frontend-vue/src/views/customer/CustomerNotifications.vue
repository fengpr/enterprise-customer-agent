<script setup lang="ts">
import { Bell, Clock, Refresh, Warning } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { customerApi } from '@/api/customer'
import type { CustomerFollowup, CustomerNotification } from '@/types/api'

const router = useRouter()
const notifications = ref<CustomerNotification[]>([])
const followups = ref<CustomerFollowup[]>([])
const loading = ref(false)
let timer: number | undefined

const unreadCount = computed(() => notifications.value.filter((item) => !item.is_read).length)

async function loadData() {
  loading.value = true
  try {
    const [notificationResponse, followupResponse] = await Promise.all([
      customerApi.notifications(),
      customerApi.followups()
    ])
    notifications.value = notificationResponse.data
    followups.value = followupResponse.data
  } finally {
    loading.value = false
  }
}

async function openNotification(item: CustomerNotification) {
  if (!item.is_read) {
    await customerApi.markNotificationRead(item.notification_id)
    item.is_read = true
  }
  await router.push({ path: '/customer/service', query: { session_id: item.session_id } })
}

async function cancelFollowup(item: CustomerFollowup) {
  try {
    await customerApi.cancelFollowup(item.followup_id)
    item.status = 'CANCELLED'
    ElMessage.success('复核任务已取消')
  } catch {
    // 统一请求拦截器负责展示业务错误，页面保留当前权威状态。
  }
}

function formatTime(value: string) {
  if (!value) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(value))
}

function statusText(status: string) {
  return ({ PENDING: '待调度', QUEUED: '已排队', RUNNING: '复核中', COMPLETED: '已完成', FAILED: '执行失败', CANCELLED: '已取消' } as Record<string, string>)[status] || status
}

onMounted(() => {
  void loadData()
  timer = window.setInterval(() => void loadData(), 30_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <main class="notification-page">
    <header class="notification-header">
      <div>
        <button class="back-link" @click="router.push('/customer')">← 返回客户首页</button>
        <h1>消息通知</h1>
        <p>物流复核结果会写回原会话；任何退货、退款动作仍需您再次确认。</p>
      </div>
      <button class="refresh-button" :disabled="loading" @click="loadData">
        <el-icon><Refresh /></el-icon> 刷新
      </button>
    </header>

    <section class="summary-grid">
      <article><el-icon><Bell /></el-icon><div><strong>{{ unreadCount }}</strong><span>未读通知</span></div></article>
      <article><el-icon><Clock /></el-icon><div><strong>{{ followups.filter((item) => ['PENDING', 'QUEUED', 'RUNNING'].includes(item.status)).length }}</strong><span>进行中复核</span></div></article>
    </section>

    <section class="panel">
      <div class="panel-title"><h2>站内通知</h2><span>点击通知可返回原会话</span></div>
      <el-empty v-if="!notifications.length && !loading" description="暂无通知" />
      <button
        v-for="item in notifications"
        :key="item.notification_id"
        class="notification-item"
        :class="{ unread: !item.is_read }"
        @click="openNotification(item)"
      >
        <span class="notice-dot" />
        <span class="notice-body"><strong>{{ item.title }}</strong><small>{{ item.content }}</small></span>
        <time>{{ formatTime(item.created_at) }}</time>
      </button>
    </section>

    <section class="panel">
      <div class="panel-title"><h2>定时复核</h2><span>默认按北京时间执行</span></div>
      <el-empty v-if="!followups.length && !loading" description="暂无复核任务" />
      <article v-for="item in followups" :key="item.followup_id" class="followup-item">
        <el-icon><Warning /></el-icon>
        <div><strong>订单 {{ item.order_no }}</strong><span>{{ formatTime(item.scheduled_at) }} · {{ statusText(item.status) }}</span></div>
        <button v-if="['PENDING', 'QUEUED'].includes(item.status)" @click="cancelFollowup(item)">取消复核</button>
      </article>
    </section>
  </main>
</template>

<style scoped>
.notification-page { min-height: 100vh; padding: 32px; background: #f5f8fd; color: #13213a; }
.notification-header { display: flex; justify-content: space-between; gap: 24px; max-width: 1120px; margin: 0 auto 22px; }
.notification-header h1 { margin: 10px 0 6px; font-size: 30px; }
.notification-header p, .panel-title span { margin: 0; color: #71809b; }
.back-link, .refresh-button, .followup-item button { border: 1px solid #d7e1ef; border-radius: 9px; background: white; color: #315a94; padding: 9px 14px; cursor: pointer; }
.refresh-button { align-self: center; display: flex; gap: 6px; align-items: center; }
.summary-grid { max-width: 1120px; margin: 0 auto 18px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.summary-grid article { display: flex; align-items: center; gap: 14px; padding: 20px; background: white; border: 1px solid #e1e8f2; border-radius: 14px; }
.summary-grid .el-icon { color: #337cf0; font-size: 28px; }.summary-grid strong { display: block; font-size: 25px; }.summary-grid span { color: #71809b; }
.panel { max-width: 1120px; margin: 0 auto 18px; background: white; border: 1px solid #e1e8f2; border-radius: 14px; overflow: hidden; }
.panel-title { display: flex; align-items: baseline; justify-content: space-between; padding: 18px 20px; border-bottom: 1px solid #edf1f7; }.panel-title h2 { margin: 0; font-size: 18px; }
.notification-item { width: 100%; display: flex; align-items: flex-start; gap: 12px; padding: 17px 20px; border: 0; border-bottom: 1px solid #edf1f7; background: white; text-align: left; cursor: pointer; }
.notification-item:hover { background: #f8fbff; }.notification-item.unread { background: #f2f7ff; }.notice-dot { width: 8px; height: 8px; margin-top: 7px; border-radius: 50%; background: #b9c5d6; }.unread .notice-dot { background: #337cf0; }
.notice-body { flex: 1; min-width: 0; }.notice-body strong, .notice-body small { display: block; }.notice-body small { margin-top: 6px; color: #52627b; line-height: 1.6; }.notification-item time { color: #8c99ad; font-size: 12px; white-space: nowrap; }
.followup-item { display: flex; gap: 12px; align-items: center; padding: 17px 20px; border-bottom: 1px solid #edf1f7; }.followup-item > div { flex: 1; }.followup-item strong, .followup-item span { display: block; }.followup-item span { margin-top: 5px; color: #71809b; font-size: 13px; }
@media (max-width: 720px) { .notification-page { padding: 18px; }.summary-grid { grid-template-columns: 1fr; }.notification-header { align-items: flex-start; }.notification-item time { display: none; } }
</style>
