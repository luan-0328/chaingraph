<script setup lang="ts">
/**
 * 订单三个状态维度标签（支付 / 履约 / 售后）。
 *
 * 为什么一定要并排显示三个？
 *   后端的 Order 有三个**互相独立**的状态维度（v2.md §10）：
 *   "已支付"不代表"已发货"，"已退款"也不代表"已取消"。
 *   合成一个"订单状态"会丢掉信息，所以前端也照原样分开展示。
 */
import type { OrderSummary } from '@/types'
import {
  FULFILLMENT_STATUS_MAP,
  ORDER_AFTER_SALE_STATUS_MAP,
  PAYMENT_STATUS_MAP,
} from '@/utils/dict'
import StatusTag from '@/components/common/StatusTag.vue'

defineProps<{ order: OrderSummary }>()
</script>

<template>
  <div class="order-status-tags">
    <StatusTag :value="order.payment_status" :map="PAYMENT_STATUS_MAP" />
    <StatusTag :value="order.fulfillment_status" :map="FULFILLMENT_STATUS_MAP" />
    <StatusTag :value="order.after_sale_status" :map="ORDER_AFTER_SALE_STATUS_MAP" />
  </div>
</template>

<style scoped>
.order-status-tags {
  display: inline-flex;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
