<script setup lang="ts">
/**
 * 人工确认弹窗（HITL，前端文档第三十二 ~ 三十四节）。
 *
 * 只有工单处于 WAITING_HUMAN 时才会被打开：
 *   Agent 已经给出处理方案（例如一笔待审核的退款），等人工点头或驳回。
 */
import { computed, ref, watch } from 'vue'

import type { TicketDetail } from '@/types/ticket'
import { formatAmount } from '@/utils/format'

const props = defineProps<{
  modelValue: boolean
  detail: TicketDetail | null
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  approve: [comment: string]
  reject: [comment: string]
}>()

const comment = ref('')

// 每次打开时清空上一次填的意见
watch(
  () => props.modelValue,
  (visible) => {
    if (visible) comment.value = ''
  },
)

const pendingRefunds = computed(
  () => props.detail?.refunds.filter((item) => item.status === 'PENDING') ?? [],
)

function close(): void {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="人工确认"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-alert
      type="warning"
      :closable="false"
      show-icon
      title="Agent 已生成处理方案，请确认是否执行"
      description="确认后系统会执行该方案（例如完成退款）并关闭工单；驳回则终止本次处理。"
    />

    <div v-if="pendingRefunds.length" class="approval-block">
      <div class="approval-block__label">待审核的退款</div>
      <el-table :data="pendingRefunds" size="small" border>
        <el-table-column label="订单号" prop="order_no" width="120" />
        <el-table-column label="退款金额" width="120">
          <template #default="{ row }">{{ formatAmount(row.amount) }}</template>
        </el-table-column>
        <el-table-column label="原因" prop="reason" show-overflow-tooltip />
      </el-table>
    </div>

    <div class="approval-block">
      <div class="approval-block__label">审批意见（可选）</div>
      <el-input
        v-model="comment"
        type="textarea"
        :rows="3"
        maxlength="200"
        show-word-limit
        placeholder="例如：确认退款 / 金额与凭证不符，请补充材料"
      />
    </div>

    <template #footer>
      <el-button :disabled="loading" @click="close">取消</el-button>
      <el-button type="danger" :loading="loading" @click="emit('reject', comment)">
        驳回
      </el-button>
      <el-button type="primary" :loading="loading" @click="emit('approve', comment)">
        确认处理
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.approval-block {
  margin-top: 16px;
}

.approval-block__label {
  color: var(--text-secondary);
  margin-bottom: 6px;
}
</style>
