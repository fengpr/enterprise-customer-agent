<script setup lang="ts">
defineOptions({ name: 'CustomerOrders' })

import { ChatDotRound, CircleCheckFilled, Document, Filter, Headset, Refresh, Search, Van } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { customerApi } from '@/api/customer'
import CustomerSidebar from '@/components/customer/CustomerSidebar.vue'
import type { ChatSession, CustomerOrder, CustomerOrderDetail, CustomerOrderLogistics } from '@/types/api'

const router = useRouter()
const route = useRoute()
const orders = ref<CustomerOrder[]>([])
const sessions = ref<ChatSession[]>([])
const selectedOrderNo = ref<string | null>(null)
const selectedDetail = ref<CustomerOrderDetail | null>(null)
const selectedLogistics = ref<CustomerOrderLogistics | null>(null)
const loading = ref(false)
const loadingDetail = ref(false)
const keyword = ref('')
const activeStatus = ref('ALL')
const dateRange = ref<[string, string] | null>(null)
const page = ref(1)
// 订单页按产品要求固定每页展示 5 条，避免用户切换条数后出现列表密度不一致。
const pageSize = 5

const statusTabs = [
  { value: 'ALL', label: '全部' }, { value: 'PENDING_PAYMENT', label: '待付款' },
  { value: 'SHIPPED', label: '待发货' }, { value: 'PENDING_RECEIPT', label: '待收货' },
  { value: 'SIGNED', label: '已完成' }, { value: 'AFTER_SALE', label: '售后中' }
]

/** 将订单与物流状态转换为客户可理解的筛选与展示状态。 */
function displayStatus(order: CustomerOrder) {
  if (order.afterSaleStatus && order.afterSaleStatus !== 'NONE') return { key: 'AFTER_SALE', label: '售后中', tone: 'after-sale' }
  if (order.orderStatus === 'PENDING_PAYMENT') return { key: 'PENDING_PAYMENT', label: '待付款', tone: 'pending' }
  if (order.orderStatus === 'SIGNED') return { key: 'SIGNED', label: '已完成', tone: 'completed' }
  if (order.orderStatus === 'SHIPPED' || order.orderStatus === 'IN_TRANSIT' || order.orderStatus === 'OUT_FOR_DELIVERY') return { key: 'PENDING_RECEIPT', label: '待收货', tone: 'shipping' }
  return { key: order.orderStatus, label: order.orderStatus || '处理中', tone: 'shipping' }
}

const filteredOrders = computed(() => orders.value.filter((order) => {
  const text = `${order.orderNo} ${order.productName || ''}`.toLowerCase()
  const status = displayStatus(order)
  const matchedKeyword = !keyword.value.trim() || text.includes(keyword.value.trim().toLowerCase())
  const matchedStatus = activeStatus.value === 'ALL' || status.key === activeStatus.value
  const paidDate = order.payTime?.slice(0, 10) || ''
  const matchedDate = !dateRange.value || (paidDate >= dateRange.value[0] && paidDate <= dateRange.value[1])
  return matchedKeyword && matchedStatus && matchedDate
}))
const pageOrders = computed(() => filteredOrders.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const metrics = computed(() => ({
  total: orders.value.length,
  pendingPayment: orders.value.filter((item) => displayStatus(item).key === 'PENDING_PAYMENT').length,
  pendingReceipt: orders.value.filter((item) => displayStatus(item).key === 'PENDING_RECEIPT').length,
  afterSale: orders.value.filter((item) => displayStatus(item).key === 'AFTER_SALE').length
}))
const selectedOrder = computed(() => orders.value.find((item) => item.orderNo === selectedOrderNo.value) || null)

function formatMoney(value?: number | string | null) { return `¥${Number(value || 0).toFixed(2)}` }
function formatTime(value?: string | null) { return value ? value.replace('T', ' ').slice(0, 16) : '—' }
function openService(order?: CustomerOrder) {
  const query = order ? { new: '1', orderNo: order.orderNo } : { new: '1' }
  void router.push({ path: '/customer/service', query })
}
function unavailable() { ElMessage.info('该功能正在建设中') }
function selectStatus(value: string) { activeStatus.value = value; page.value = 1 }
function resetFilters() { keyword.value = ''; activeStatus.value = 'ALL'; dateRange.value = null; page.value = 1 }

/** 订单详情和物流仅跟随当前选中订单刷新，避免列表翻页造成不必要请求。 */
async function selectOrder(orderNo: string) {
  if (selectedOrderNo.value === orderNo && selectedDetail.value) return
  selectedOrderNo.value = orderNo
  selectedDetail.value = null
  selectedLogistics.value = null
  loadingDetail.value = true
  try {
    const [detailResult, logisticsResult] = await Promise.allSettled([
      customerApi.orderDetail(orderNo), customerApi.orderLogistics(orderNo)
    ])
    if (detailResult.status === 'fulfilled') selectedDetail.value = detailResult.value.data
    if (logisticsResult.status === 'fulfilled') selectedLogistics.value = logisticsResult.value.data
  } finally {
    loadingDetail.value = false
  }
}

async function loadPage(showFeedback = false) {
  if (loading.value) return
  loading.value = true
  try {
    const [orderResult, sessionResult] = await Promise.all([customerApi.orders(), customerApi.sessions(50)])
    orders.value = orderResult.data
    sessions.value = sessionResult.data
    // 首页携带订单号时只在当前客户订单集合中定位，避免信任外部 URL 参数。
    const targetOrderNo = typeof route.query.orderNo === 'string' ? route.query.orderNo : selectedOrderNo.value
    const target = orders.value.find((item) => item.orderNo === targetOrderNo) || orders.value[0]
    if (target) await selectOrder(target.orderNo)
    if (showFeedback) ElMessage.success('订单数据已刷新')
  } catch {
    // 请求拦截器已统一提示错误，页面保留上一次可见数据以避免切换闪白。
  } finally {
    loading.value = false
  }
}

watch([keyword, activeStatus, dateRange], () => { page.value = 1 })
watch(() => route.query.orderNo, (orderNo) => {
  if (typeof orderNo === 'string' && orders.value.some((item) => item.orderNo === orderNo)) void selectOrder(orderNo)
})
onMounted(() => { void loadPage() })
onActivated(() => { if (!orders.value.length) void loadPage() })
</script>

<template>
  <div class="customer-orders-page">
    <CustomerSidebar :sessions="sessions" @contact-service="openService()" @unavailable="unavailable" @select-session="openService()" @sessions-changed="() => loadPage()" />
    <main class="customer-orders-main">
      <header class="orders-header">
        <div><h1>我的订单</h1><p>查看订单状态、物流进度、售后入口与订单详情</p></div>
        <div class="orders-header-actions"><el-button :icon="Refresh" :loading="loading" @click="loadPage(true)">刷新订单</el-button><el-button :icon="Filter" @click="unavailable">筛选订单</el-button><el-button type="primary" :icon="ChatDotRound" @click="openService()">新建咨询</el-button></div>
      </header>

      <section class="order-metric-grid">
        <article><i class="blue"><Document /></i><span>全部订单</span><b>{{ metrics.total }}</b><small>当前账户全部订单</small></article>
        <article><i class="orange">¥</i><span>待付款</span><b>{{ metrics.pendingPayment }}</b><small>待支付的订单</small></article>
        <article><i class="green"><Van /></i><span>待收货</span><b>{{ metrics.pendingReceipt }}</b><small>等待签收的订单</small></article>
        <article><i class="purple">◆</i><span>售后中</span><b>{{ metrics.afterSale }}</b><small>售后/退换货处理中</small></article>
      </section>

      <section class="orders-workspace">
        <div class="orders-left">
          <section class="orders-filter-card">
            <el-input v-model="keyword" :prefix-icon="Search" placeholder="搜索订单号 / 商品名称" clearable />
            <div class="status-tabs"><button v-for="tab in statusTabs" :key="tab.value" :class="{ active: activeStatus === tab.value }" @click="selectStatus(tab.value)">{{ tab.label }}</button></div>
            <div class="filter-controls"><el-date-picker v-model="dateRange" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" /><el-button @click="resetFilters">重置</el-button><el-button type="primary" @click="page = 1">筛选</el-button></div>
          </section>

          <section class="orders-list-card" v-loading="loading">
            <div class="orders-list-title"><h2>订单列表 <small>（共 {{ filteredOrders.length }} 个订单）</small></h2></div>
            <div v-if="pageOrders.length" class="order-list">
              <article v-for="order in pageOrders" :key="order.orderNo" :class="['order-row', { selected: selectedOrderNo === order.orderNo }]" @click="selectOrder(order.orderNo)">
                <div class="order-product-placeholder"><Van /></div>
                <div class="order-ident"><small>订单号：{{ order.orderNo }}　 下单时间：{{ formatTime(order.payTime) }}</small><b>{{ order.productName || '商品信息暂不可用' }}</b><span>{{ order.productCategory || '智能硬件' }}　x{{ order.quantity }}</span></div>
                <div class="order-price"><b>{{ formatMoney(order.amount) }}</b><small>订单金额</small></div>
                <em :class="displayStatus(order).tone">{{ displayStatus(order).label }}</em>
                <div class="order-actions"><el-button size="small" @click.stop="selectOrder(order.orderNo)">查看详情</el-button><el-button v-if="displayStatus(order).key === 'PENDING_RECEIPT'" size="small" type="primary" plain @click.stop="selectOrder(order.orderNo)">查看物流</el-button><el-button v-else size="small" type="primary" plain @click.stop="openService(order)">联系客服</el-button></div>
              </article>
            </div>
            <el-empty v-else description="没有匹配的订单" :image-size="72" />
            <div class="orders-pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" layout="prev, pager, next" :total="filteredOrders.length" /></div>
          </section>

          <section class="orders-quick-services"><h2>快捷服务</h2><button @click="selectedOrder && selectOrder(selectedOrder.orderNo)"><Van /><span>查询物流</span><small>查看物流跟踪进度</small></button><button @click="unavailable"><i>◉</i><span>申请退货退款</span><small>退货/退款申请入口</small></button><button @click="openService(selectedOrder || undefined)"><Headset /><span>联系客服</span><small>联系在线智能客服</small></button><button @click="unavailable"><i>▣</i><span>发票问题</span><small>发票申请与咨询</small></button></section>
        </div>

        <aside class="order-detail-panel" v-loading="loadingDetail">
          <template v-if="selectedOrder">
            <h2>订单详情</h2><p class="detail-order-no">订单号 <b>{{ selectedOrder.orderNo }}</b><em :class="displayStatus(selectedOrder).tone">{{ displayStatus(selectedOrder).label }}</em></p><p class="detail-time">下单时间：{{ formatTime(selectedOrder.payTime) }}</p>
            <section class="detail-section"><h3>订单进度</h3><div class="order-progress"><div class="done"><i><CircleCheckFilled /></i><span>已下单<small>{{ formatTime(selectedOrder.payTime) }}</small></span></div><div :class="{ done: Boolean(selectedOrder.shipTime) }"><i><CircleCheckFilled /></i><span>已发货<small>{{ formatTime(selectedOrder.shipTime) }}</small></span></div><div :class="{ done: Boolean(selectedOrder.signTime) }"><i><CircleCheckFilled /></i><span>{{ selectedOrder.signTime ? '已签收' : '运输中' }}<small>{{ formatTime(selectedOrder.signTime || selectedLogistics?.estimatedDeliveryTime) }}</small></span></div></div></section>
            <section class="detail-section"><h3>物流信息</h3><p v-if="selectedLogistics" class="detail-line"><span>{{ selectedLogistics.carrierName || '物流服务商' }}</span>{{ selectedLogistics.trackingNo || '运单号暂未同步' }}</p><div v-if="selectedLogistics?.traces?.length" class="logistics-traces"><div v-for="trace in selectedLogistics.traces" :key="`${trace.status}-${trace.occurredAt}`"><i></i><time>{{ formatTime(trace.occurredAt) }}</time><p>{{ trace.description }}</p></div></div><p v-else class="detail-muted">暂无可展示的物流轨迹</p></section>
            <section class="detail-section"><h3>收货信息</h3><p class="detail-line"><span>收货人</span>{{ selectedDetail?.receiverName || '—' }}　{{ selectedDetail?.receiverPhoneMasked || '' }}</p><p class="detail-line"><span>收货地址</span>{{ selectedDetail?.shippingAddress || '—' }}</p><p class="detail-line"><span>支付方式</span>{{ selectedDetail?.paymentMethod || '—' }}</p><p class="detail-line"><span>配送方式</span>{{ selectedDetail?.deliveryMethod || '—' }}</p><p class="detail-line"><span>运单号</span>{{ selectedLogistics?.trackingNo || '暂未同步' }}</p></section>
            <el-button class="detail-contact" type="primary" plain @click="openService(selectedOrder)">联系客服</el-button>
          </template>
          <el-empty v-else description="请选择一笔订单" />
        </aside>
      </section>
    </main>
  </div>
</template>
