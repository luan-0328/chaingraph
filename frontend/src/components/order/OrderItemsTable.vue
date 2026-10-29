<script setup lang="ts">
/**
 * 订单明细表：展示 OrderItemInfo[]（下单时写入的商品快照）。
 *
 * 注意用的是订单里的快照字段（product_name / product_price），
 * 而不是再去查商品接口 —— 商品改名/改价后历史订单必须保持原样。
 */
import type { OrderItemInfo } from '@/types'
import { formatAmount } from '@/utils/format'

defineProps<{ items: OrderItemInfo[] }>()
</script>

<template>
  <el-table :data="items" border size="default" class="order-items-table">
    <el-table-column prop="product_name" label="商品" min-width="220" show-overflow-tooltip />

    <el-table-column label="单价" width="120" align="right">
      <template #default="{ row }">{{ formatAmount(row.product_price) }}</template>
    </el-table-column>

    <el-table-column prop="quantity" label="数量" width="100" align="center" />

    <el-table-column label="小计" width="140" align="right">
      <template #default="{ row }">
        <span class="text-price">{{ formatAmount(row.subtotal) }}</span>
      </template>
    </el-table-column>

    <template #empty>
      <el-empty description="暂无商品明细" :image-size="60" />
    </template>
  </el-table>
</template>

<style scoped>
.order-items-table {
  width: 100%;
}
</style>
