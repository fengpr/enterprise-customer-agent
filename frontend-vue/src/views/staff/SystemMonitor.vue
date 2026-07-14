<script setup lang="ts">
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { systemMonitorApi } from '@/api/staff'
import type { SystemMonitorCacheMetric, SystemMonitorSnapshot } from '@/types/api'

const router = useRouter()
const loading = ref(false)
const errorMessage = ref('')
const snapshot = ref<SystemMonitorSnapshot | null>(null)
const lastRefreshAt = ref('')
let refreshTimer: ReturnType<typeof setInterval> | null = null

const queueWarning = computed(() => {
  const data = snapshot.value
  if (!data) return false
  return !data.queue.available || !data.worker.active || Number(data.queue.pending || 0) > 0 || Number(data.dlq.count || 0) > 0
})

const llmWarning = computed(() => {
  const llm = snapshot.value?.llm
  if (!llm) return false
  return Number(llm.timeout || 0) > 0 || Number(llm.rate_limit_429 || 0) > 0 || Number(llm.circuit_open || 0) > 0
})

const cacheCards = computed(() => {
  const cache = snapshot.value?.cache || {}
  return [
    ['RAG 检索缓存', cache.rag],
    ['订单读取缓存', cache.order],
    ['工单读取缓存', cache.ticket],
    ['身份缓存', cache.identity],
    ['会话缓存', cache.session]
  ] as Array<[string, SystemMonitorCacheMetric | undefined]>
})

function formatCount(value: number | null | undefined) {
  return value == null ? '不可用' : String(value)
}

function formatRate(value: number | null | undefined) {
  return value == null ? '暂无样本' : `${Math.round(value * 100)}%`
}

function cardType(ok: boolean, warn = false) {
  if (!ok) return 'danger'
  if (warn) return 'warning'
  return 'success'
}

async function loadSnapshot(silent = false) {
  if (!silent) loading.value = true
  errorMessage.value = ''
  try {
    const { data } = await systemMonitorApi.snapshot()
    snapshot.value = data
    lastRefreshAt.value = new Date().toLocaleString('zh-CN', { hour12: false })
  } catch {
    errorMessage.value = '系统监控接口不可用，请确认 Agent 服务已启动且当前账号为 staff。'
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadSnapshot()
  // 内部监控页默认 10 秒刷新一次，离开页面时会清理定时器。
  refreshTimer = setInterval(() => loadSnapshot(true), 10_000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <main class="monitor-page">
    <header class="monitor-header">
      <div>
        <el-button :icon="ArrowLeft" text @click="router.push('/staff')">返回坐席工作台</el-button>
        <h1>系统监控</h1>
        <p>轻量展示在线 Agent、可靠队列、Worker、LLM 异常、DLQ 和缓存命中率；生产多副本仍以 Grafana 为权威大盘。</p>
      </div>
      <div class="header-actions">
        <span class="updated-at">最近刷新：{{ lastRefreshAt || '-' }}</span>
        <el-button :icon="Refresh" :loading="loading" type="primary" @click="loadSnapshot()">刷新</el-button>
      </div>
    </header>

    <el-result v-if="errorMessage && !snapshot" icon="error" title="监控加载失败" :sub-title="errorMessage">
      <template #extra>
        <el-button :icon="Refresh" type="primary" @click="loadSnapshot()">重试</el-button>
      </template>
    </el-result>

    <template v-else>
      <el-alert
        v-if="errorMessage"
        class="monitor-alert"
        type="warning"
        show-icon
        :closable="false"
        :title="errorMessage"
      />

      <section class="card-grid">
        <el-card shadow="never" class="status-card">
          <span>Worker 状态</span>
          <strong :class="{ danger: !snapshot?.worker.active }">{{ snapshot?.worker.active ? '在线' : '离线' }}</strong>
          <el-tag :type="cardType(Boolean(snapshot?.worker.active))">
            {{ snapshot?.worker.active ? '可消费队列任务' : '需要启动 agent-worker' }}
          </el-tag>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>队列积压</span>
          <strong>{{ formatCount(snapshot?.queue.stream_depth) }}</strong>
          <small>Redis Stream depth</small>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>Pending</span>
          <strong :class="{ warning: Number(snapshot?.queue.pending || 0) > 0 }">{{ formatCount(snapshot?.queue.pending) }}</strong>
          <small>消费者已领取但未 ACK</small>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>DLQ</span>
          <strong :class="{ danger: Number(snapshot?.dlq.count || 0) > 0 }">{{ formatCount(snapshot?.dlq.count) }}</strong>
          <small>超过最大重试的死信任务</small>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>Running / Retry</span>
          <strong>{{ formatCount(snapshot?.queue.running) }} / {{ formatCount(snapshot?.queue.retrying) }}</strong>
          <small>执行中与重试中的任务</small>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>降级次数</span>
          <strong :class="{ warning: Number(snapshot?.degraded.total || 0) > 0 }">{{ formatCount(snapshot?.degraded.total) }}</strong>
          <small>安全降级总数</small>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>LLM 异常</span>
          <strong :class="{ warning: llmWarning }">
            {{ formatCount(snapshot?.llm.timeout) }} / {{ formatCount(snapshot?.llm.rate_limit_429) }} / {{ formatCount(snapshot?.llm.circuit_open) }}
          </strong>
          <small>timeout / 429 / circuit_open</small>
        </el-card>

        <el-card shadow="never" class="status-card">
          <span>队列健康</span>
          <strong :class="{ danger: queueWarning }">{{ queueWarning ? '需关注' : '正常' }}</strong>
          <el-tag :type="cardType(!queueWarning, queueWarning)">{{ snapshot?.queue.error || '队列可读' }}</el-tag>
        </el-card>
      </section>

      <section class="detail-grid">
        <el-card shadow="never">
          <template #header>缓存命中率</template>
          <el-table :data="cacheCards" empty-text="暂无缓存样本">
            <el-table-column label="缓存类型" min-width="160">
              <template #default="{ row }">{{ row[0] }}</template>
            </el-table-column>
            <el-table-column label="命中率" width="120">
              <template #default="{ row }">{{ formatRate(row[1]?.hit_rate) }}</template>
            </el-table-column>
            <el-table-column label="Hit / Miss / Error" min-width="180">
              <template #default="{ row }">
                {{ row[1]?.hit ?? 0 }} / {{ row[1]?.miss ?? 0 }} / {{ row[1]?.error ?? 0 }}
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never">
          <template #header>运行摘要</template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="Agent">{{ snapshot?.agent_status.status || '-' }}</el-descriptions-item>
            <el-descriptions-item label="模型 Provider">{{ snapshot?.agent_status.llm?.provider || '未配置' }}</el-descriptions-item>
            <el-descriptions-item label="模型名称">{{ snapshot?.agent_status.llm?.model || '-' }}</el-descriptions-item>
            <el-descriptions-item label="队列可用">{{ snapshot?.queue.available ? '是' : '否' }}</el-descriptions-item>
            <el-descriptions-item label="队列错误">{{ snapshot?.queue.error || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </section>
    </template>
  </main>
</template>

<style scoped>
.monitor-page { min-height: 100vh; padding: 28px; background: #f6f8fc; }
.monitor-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 22px; }
.monitor-header h1 { margin: 8px 0; color: #172033; }
.monitor-header p, .updated-at, .status-card span, .status-card small { color: #64748b; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.monitor-alert { margin-bottom: 16px; }
.card-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.status-card { min-height: 140px; }
.status-card :deep(.el-card__body) { display: grid; gap: 10px; }
.status-card strong { color: #0b66ff; font-size: 28px; line-height: 1.15; }
.status-card strong.warning { color: #d97706; }
.status-card strong.danger { color: #dc2626; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr); gap: 16px; margin-top: 16px; }
@media (max-width: 1200px) {
  .card-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .detail-grid { grid-template-columns: 1fr; }
  .monitor-header { flex-direction: column; }
}
</style>
