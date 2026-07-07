<script setup lang="ts">
import { Check, Phone, Search, Tools } from '@element-plus/icons-vue'
import type { CustomerOrder } from '@/types/api'
import StatusTag from './StatusTag.vue'

defineProps<{
  orders: CustomerOrder[]
  selectedOrderNo: string | null
  loading?: boolean
  collapsed?: boolean
}>()

const emit = defineEmits<{
  select: [orderNo: string]
  afterSale: [orderNo: string]
  contact: [orderNo: string]
  toggleCollapse: []
}>()

function productVisual(name?: string | null) {
  if (name?.includes('Router')) return '📶'
  if (name?.includes('Mesh')) return '▯'
  if (name?.includes('交换机')) return '▭'
  return '◻'
}
</script>

<template>
  <section :class="['orders-card', 'dashboard-card', { 'is-collapsed': collapsed }]">
    <div class="module-title">
      <h2>我的订单 <span>({{ orders.length }})</span></h2>
      <a>查看全部订单 ›</a>
      <div class="module-title-actions">
        <el-tooltip :content="collapsed ? '展开订单区' : '收起订单区'" placement="top">
          <el-button class="collapse-button" text @click="emit('toggleCollapse')">
            {{ collapsed ? '+' : '-' }}
          </el-button>
        </el-tooltip>
      </div>
    </div>
    <el-skeleton v-if="!collapsed && loading" :rows="3" animated />
    <div v-else-if="!collapsed" class="order-card-grid">
      <article
        v-for="order in orders.slice(0, 3)"
        :key="order.orderNo"
        :class="['modern-order-card', { active: order.orderNo === selectedOrderNo }]"
        role="button"
        tabindex="0"
        @click="emit('select', order.orderNo)"
        @keydown.enter="emit('select', order.orderNo)"
      >
        <span v-if="order.orderNo === selectedOrderNo" class="selected-pill">
          <el-icon><Check /></el-icon>
          当前咨询订单
        </span>
        <span v-else class="select-hint">点击选择</span>
        <div class="product-visual">{{ productVisual(order.productName) }}</div>
        <div class="order-body">
          <h3>{{ order.productName || order.orderNo }}</h3>
          <p>订单号：{{ order.orderNo }}</p>
          <StatusTag :status="order.orderStatus" />
          <strong>￥{{ order.amount ?? '-' }}</strong>
          <span>下单时间：{{ String(order.payTime || order.signTime || '-').replace('T', ' ').slice(0, 16) }}</span>
        </div>
        <div class="order-actions">
          <el-button :icon="Search" @click.stop>查看详情</el-button>
          <el-button :icon="Tools" @click.stop="emit('afterSale', order.orderNo)">申请售后</el-button>
          <el-button :icon="Phone" type="primary" @click.stop="emit('contact', order.orderNo)">联系客服</el-button>
        </div>
      </article>
      <el-empty v-if="!orders.length" description="暂无订单" />
    </div>
  </section>
</template>
