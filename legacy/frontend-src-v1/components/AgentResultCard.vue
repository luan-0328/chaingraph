<script setup lang="ts">
/**
 * Agent 处理结果卡片。
 *
 * 只展示**业务结果**：处理结论、本次调用过的业务动作、人工意见、退款记录。
 * 不展示 LLM 的思维链（前端文档第二十二节 / 第五十一节）。
 *
 * 关于 toolsUsed：
 *   后端没有把"工具调用轨迹"持久化到 tickets 表（只存最终结论），
 *   所以这里只能展示**本次会话**里 Agent 实际调用了哪些工具
 *   （由工单详情页从 SSE 的 done 事件 / process 响应里传进来）。
 *   刷新页面后这一块会消失 —— 这是 V1 的已知取舍。
 */
import { computed } from 'vue'

import type { TicketDetail } from '@/types/ticket'
import { refundStatusLabel, toolLabel } from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    detail: TicketDetail
    toolsUsed?: string[]
  }>(),
  { toolsUsed: () => [] },
)

const hasResult = computed(() => Boolean(props.detail.agent_result))
const refunds = computed(() => props.detail.refunds)
</script>

<template>
  <div class="page-card">
    <h3 class="page-card__title">Agent 处理结果</h3>

    <el-empty
      v-if="!hasResult"
      description="暂无处理结果，点击下方「开始处理」让 Agent 分析这张工单"
    />

    <template v-else>
      <div class="result-block">
        <div class="result-block__label">处理结论</div>
        <div class="result-block__body pre-wrap">{{ detail.agent_result }}</div>
      </div>

      <div v-if="toolsUsed.length" class="result-block">
        <div class="result-block__label">本次执行的操作</div>
        <el-tag
          v-for="tool in toolsUsed"
          :key="tool"
          class="result-block__tool"
          type="info"
          effect="plain"
        >
          {{ toolLabel(tool) }}
        </el-tag>
      </div>

      <div v-if="detail.resolution" class="result-block">
        <div class="result-block__label">人工处理意见</div>
        <div class="result-block__body pre-wrap">{{ detail.resolution }}</div>
      </div>

      <div v-if="refunds.length" class="result-block">
        <div class="result-block__label">退款记录</div>
        <el-table :data="refunds" size="small" border>
          <el-table-column label="退款单号" prop="refund_id" width="100" />
          <el-table-column label="订单号" prop="order_no" width="120" />
          <el-table-column label="金额" width="120">
            <template #default="{ row }">{{ formatAmount(row.amount) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">{{ refundStatusLabel(row.status) }}</template>
          </el-table-column>
          <el-table-column label="申请时间" width="180">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="原因" prop="reason" show-overflow-tooltip />
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.result-block + .result-block {
  margin-top: 16px;
}

.result-block__label {
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.result-block__body {
  color: var(--text-regular);
  line-height: 1.8;
}

.result-block__tool {
  margin-right: 8px;
}
</style>
