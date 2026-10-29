<script setup lang="ts">
/** 工单基础信息卡片（工单 ID / 类型 / 状态 / 时间 / 问题描述） */
import { computed } from 'vue'

import TicketStatusTag from '@/components/TicketStatusTag.vue'
import type { TicketDetail } from '@/types/ticket'
import { ticketTypeLabel } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ detail: TicketDetail }>()

const refundSummary = computed(() => {
  if (props.detail.refunds.length === 0) return '无'
  return props.detail.refunds
    .map((item) => `${item.order_no ?? item.order_id}（¥${item.amount.toFixed(2)}）`)
    .join('、')
})
</script>

<template>
  <div class="page-card">
    <h3 class="page-card__title">工单信息</h3>

    <el-descriptions :column="2" border size="default">
      <el-descriptions-item label="工单编号"> #{{ detail.ticket_id }} </el-descriptions-item>

      <el-descriptions-item label="当前状态">
        <TicketStatusTag :status="detail.status" />
      </el-descriptions-item>

      <el-descriptions-item label="工单类型">
        {{ ticketTypeLabel(detail.type) }}
      </el-descriptions-item>

      <el-descriptions-item label="提交人"> 用户 #{{ detail.user_id }} </el-descriptions-item>

      <el-descriptions-item label="创建时间">
        {{ formatDateTime(detail.created_at) }}
      </el-descriptions-item>

      <el-descriptions-item label="更新时间">
        {{ formatDateTime(detail.updated_at) }}
      </el-descriptions-item>

      <el-descriptions-item label="关联订单" :span="2">
        {{ refundSummary }}
      </el-descriptions-item>
    </el-descriptions>

    <div class="ticket-content">
      <div class="ticket-content__label">问题描述</div>
      <div class="ticket-content__body pre-wrap">{{ detail.content }}</div>
    </div>
  </div>
</template>

<style scoped>
.ticket-content {
  margin-top: 16px;
}

.ticket-content__label {
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.ticket-content__body {
  background: #fafafa;
  border: 1px solid var(--border-color);
  border-radius: 4px;
  padding: 12px;
  min-height: 60px;
  color: var(--text-regular);
}
</style>
