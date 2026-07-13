<script setup lang="ts">
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ragEvaluationApi } from '@/api/staff'
import type { OnlineEvaluationReport, RagEvaluationReport, RagEvaluationRow } from '@/types/api'

const router = useRouter()
const loading = ref(false)
const report = ref<RagEvaluationReport | null>(null)
const updatedAt = ref('')
const errorMessage = ref('')
const agentJobStatus = ref('')
const agentJobRunning = ref(false)
const goldenSampleCount = ref<number | null>(null)
const onlineReport = ref<OnlineEvaluationReport | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null

const metricCards = computed(() => {
  if (!report.value) return []
  const metrics = report.value.metrics
  if (report.value.evaluation_mode === 'baseline') {
    return [
      ['检索 Hit@1', metrics['hit@1'], '首条结果命中预期知识文档'],
      ['检索 Hit@3', metrics['hit@3'], '前 3 条结果命中预期知识文档'],
      ['上下文精确率', metrics.context_precision, '召回片段中预期知识的占比'],
      ['上下文召回率', metrics.context_recall, '是否召回预期知识文档'],
      ['知识集合准确率', metrics.collection_accuracy, '首条结果是否进入预期知识集合'],
      ['业务域准确率', metrics.scope_accuracy, '首条结果是否属于预期业务域'],
      ['风险等级准确率', metrics.risk_accuracy, '首条结果风险等级是否正确'],
      ['关键事实覆盖（诊断）', metrics.must_contain_hit_rate, '用于发现知识文档与样本标注差异，不计入失败']
    ]
  }
  return [
    ['上下文精确率', metrics.context_precision, '召回片段中预期知识的占比'],
    ['上下文召回率', metrics.context_recall, '是否召回预期知识文档'],
    ['Faithfulness', metrics.faithfulness, '回答事实是否受检索证据支持'],
    ['Answer Relevance', metrics.answer_relevance, '回答是否聚焦用户问题'],
    ['Answer Correctness', metrics.answer_correctness, '回答是否正确且未遗漏关键事实'],
    ['Semantic Similarity', metrics.semantic_similarity, '与参考答案或要求事实的相似度'],
    ['引用精确率', metrics.citation_precision, '引用的片段能够支撑回答结论'],
    ['必需事实覆盖', metrics.required_fact_coverage, '回答覆盖评测样本要求的事实']
  ]
})

function percentage(value: number | null | undefined) {
  if (value == null) return '不适用'
  return `${Math.round((value || 0) * 100)}%`
}

function unsupportedClaims(row: RagEvaluationRow) {
  return row.citation_validation.unsupported_claims.map((item) => item.claim).join('；') || '-'
}

async function loadReport() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [baseline, online] = await Promise.all([ragEvaluationApi.report(), ragEvaluationApi.onlineReport()])
    const data = baseline.data
    report.value = data
    onlineReport.value = online.data
    updatedAt.value = new Date().toLocaleString('zh-CN', { hour12: false })
  } catch (error) {
    report.value = null
    errorMessage.value = '无法获取 RAG 基线评测报告，请确认 Agent 服务已启动。'
  } finally {
    loading.value = false
  }
}

async function startAgentEvaluation() {
  errorMessage.value = ''
  agentJobRunning.value = true
  try {
    const { data } = await ragEvaluationApi.createJob(goldenSampleCount.value)
    const label = goldenSampleCount.value ? `${goldenSampleCount.value} 条` : '全部样本'
    agentJobStatus.value = `真实 Agent 评测已排队，将由后台执行 ${label}。`
    pollAgentJob(data.job_id)
  } catch {
    agentJobRunning.value = false
    errorMessage.value = '无法创建真实 Agent 评测任务，请稍后重试。'
  }
}

async function pollAgentJob(jobId: string) {
  try {
    const { data } = await ragEvaluationApi.getJob(jobId)
    if (data.status === 'SUCCEEDED' && data.report) {
      report.value = data.report
      updatedAt.value = new Date().toLocaleString('zh-CN', { hour12: false })
      agentJobStatus.value = '真实 Agent 全量评测已完成。'
      agentJobRunning.value = false
      return
    }
    if (data.status === 'FAILED') {
      agentJobStatus.value = ''
      agentJobRunning.value = false
      errorMessage.value = `真实 Agent 评测失败：${data.error || '未知错误'}`
      return
    }
    agentJobStatus.value = data.status === 'PROCESSING' ? '真实 Agent 全量评测进行中，请保持页面开启。' : '真实 Agent 全量评测正在等待独立 Worker 领取。'
    pollTimer = setTimeout(() => pollAgentJob(jobId), 2000)
  } catch {
    agentJobStatus.value = ''
    agentJobRunning.value = false
    errorMessage.value = '评测任务状态查询失败。'
  }
}

onMounted(loadReport)
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
})
</script>

<template>
  <main class="rag-page">
    <header class="rag-header">
      <div>
        <el-button :icon="ArrowLeft" text @click="router.push('/staff')">返回坐席工作台</el-button>
        <h1>RAG 质量评测</h1>
        <p>默认展示全量快速基线；真实 Agent 全量评测在后台执行 40 条样本，不阻塞页面请求。</p>
      </div>
      <div class="header-buttons">
        <el-button :loading="loading" @click="loadReport">全量基线</el-button>
        <el-input-number v-model="goldenSampleCount" :min="1" :max="500" :disabled="agentJobRunning" controls-position="right" placeholder="全部" />
        <el-button :disabled="agentJobRunning" type="primary" @click="startAgentEvaluation">真实 Agent 全量评测</el-button>
      </div>
    </header>

    <el-skeleton v-if="loading && !report" :rows="8" animated />
    <el-result v-else-if="errorMessage" icon="error" title="评测加载失败" :sub-title="errorMessage">
      <template #extra><el-button :icon="Refresh" type="primary" @click="loadReport">重试基线评测</el-button></template>
    </el-result>
    <template v-else-if="report">
      <el-alert v-if="agentJobStatus" :title="agentJobStatus" :closable="false" class="job-status" type="info" show-icon />
      <section class="metric-grid">
        <el-card v-for="([label, value, hint]) in metricCards" :key="label" shadow="never" class="metric-card">
          <span>{{ label }}</span>
          <strong>{{ percentage(value as number) }}</strong>
          <small>{{ hint }}</small>
          <el-progress :percentage="Math.round((value as number) * 100)" :show-text="false" />
        </el-card>
      </section>

      <section class="summary-grid">
        <el-card shadow="never">
          <template #header>评测概览</template>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="评测样本">{{ report.metrics.total }}</el-descriptions-item>
            <el-descriptions-item label="失败样本">{{ report.metrics.failed_count }}</el-descriptions-item>
            <el-descriptions-item label="无召回样本">{{ report.metrics.no_hit_count }}</el-descriptions-item>
            <el-descriptions-item label="幻觉样本">
              <el-tag :type="report.metrics.hallucination_count ? 'danger' : 'info'">{{ report.metrics.hallucination_count ?? '不适用' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="知识集合准确率">{{ percentage(report.metrics.collection_accuracy) }}</el-descriptions-item>
            <el-descriptions-item label="业务域准确率">{{ percentage(report.metrics.scope_accuracy) }}</el-descriptions-item>
            <el-descriptions-item label="平均生成延迟">{{ report.metrics.avg_generation_latency_ms == null ? '不适用' : `${report.metrics.avg_generation_latency_ms} ms` }}</el-descriptions-item>
            <el-descriptions-item label="拒答准确率">{{ report.metrics.refusal_accuracy == null ? '暂无拒答样本' : percentage(report.metrics.refusal_accuracy) }}</el-descriptions-item>
            <el-descriptions-item label="估算模型成本">{{ report.metrics.cost_measured_count ? report.metrics.estimated_cost_total : '不适用' }}</el-descriptions-item>
            <el-descriptions-item v-if="report.evaluation_mode === 'agent'" label="DeepEval Judge 异常">
              <el-tag :type="report.metrics.deepeval_error_count ? 'warning' : 'success'">
                {{ report.metrics.deepeval_error_count || 0 }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
          <p class="updated-at">最近执行：{{ updatedAt }}</p>
        </el-card>
        <el-alert
          title="评测说明"
          type="info"
          :closable="false"
          show-icon
          description="失败明细用于定位是检索偏差、引用无效、必需事实遗漏，还是回答超出本轮知识证据。"
        />
      </section>

      <section v-if="onlineReport" class="summary-grid">
        <el-card shadow="never">
          <template #header>线上质量监控（近 7 天）</template>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="已评测 Trace">{{ onlineReport.metrics.evaluated_count || 0 }}</el-descriptions-item>
            <el-descriptions-item label="失败 Trace">{{ onlineReport.metrics.failure_count || 0 }}</el-descriptions-item>
            <el-descriptions-item label="Faithfulness">{{ percentage(onlineReport.metrics.faithfulness ?? undefined) }}</el-descriptions-item>
            <el-descriptions-item label="Answer Relevancy">{{ percentage(onlineReport.metrics.answer_relevancy ?? undefined) }}</el-descriptions-item>
            <el-descriptions-item label="队列待处理">{{ onlineReport.queue.counts.PENDING || 0 }}</el-descriptions-item>
            <el-descriptions-item label="每日预算">{{ onlineReport.queue.budget_used }} / {{ onlineReport.queue.daily_budget }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
        <el-alert title="线上 Trace 仅异步评测" type="success" :closable="false" show-icon description="客户请求只记录脱敏 Trace；DeepEval 由独立 Worker 执行，不会增加客户回复延迟。" />
      </section>

      <el-card shadow="never" class="failure-card">
        <template #header>失败样本与诊断</template>
        <el-table :data="report.failures" max-height="460" empty-text="全部样本通过">
          <el-table-column label="用户问题" min-width="180" prop="sample.query" />
          <el-table-column label="失败原因" min-width="190">
            <template #default="{ row }"><el-tag v-for="item in row.failures" :key="item" class="failure-tag" type="danger">{{ item }}</el-tag></template>
          </el-table-column>
          <el-table-column label="有据率" width="100">
            <template #default="{ row }">{{ percentage(row.citation_validation.groundedness) }}</template>
          </el-table-column>
          <el-table-column label="生成回答" min-width="320">
            <template #default="{ row }"><span class="direct-answer">{{ row.generated_answer || '-' }}</span></template>
          </el-table-column>
          <el-table-column label="未支持断言" min-width="240">
            <template #default="{ row }">{{ unsupportedClaims(row) }}</template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </main>
</template>

<style scoped>
.rag-page { min-height: 100vh; padding: 28px; background: #f6f8fc; }
.rag-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 24px; }.header-buttons { display: flex; gap: 10px; }
.rag-header h1 { margin: 8px 0; color: #172033; }.rag-header p, .updated-at { color: #64748b; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.metric-card { display: grid; gap: 10px; }.metric-card span, .metric-card small { color: #64748b; }.metric-card strong { color: #0b66ff; font-size: 30px; }
.summary-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); gap: 16px; margin-top: 16px; }.failure-card, .job-status { margin-top: 16px; }
.failure-tag { margin: 2px; }.updated-at { margin-bottom: 0; font-size: 13px; }
.direct-answer { color: #475569; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
@media (max-width: 1100px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }.rag-header { align-items: flex-start; flex-direction: column; }
</style>
