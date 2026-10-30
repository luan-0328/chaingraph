<script setup lang="ts">
/**
 * 人工审批弹窗（HITL，Human-In-The-Loop）。
 *
 * 为什么需要它：Agent 在 LangGraph 里跑到"会真正动钱 / 动业务状态"的节点时会 interrupt()
 * 停下来，工单进入 WAITING_HUMAN。只有客服在这里明确点"批准"或"驳回"，流程才会继续：
 *   批准 = 恢复被中断的图继续执行（例如真正发起退款、售后完成、订单转 REFUNDED）；
 *   驳回 = 结束本次 Agent 流程，工单转由客服人工处理。
 *
 * 职责边界：本组件只做"展示 Agent 方案 + 收集审批意见"，接口调用交给父页面。
 * 这样弹窗不持有业务状态、重试/刷新的逻辑只有一份，管理端也能直接复用这个弹窗。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { formatRefundReview } from '@/utils/format'

import StatusTag from '@/components/common/StatusTag.vue'
import type { TicketDetail } from '@/types'
import { TICKET_STATUS_MAP, TICKET_TYPE_MAP } from '@/utils/dict'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    ticket: TicketDetail | null
    loading?: boolean
  }>(),
  { loading: false },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'approve', comment: string): void
  (e: 'reject', reason: string): void
}>()

/** 审批意见：批准时当备注（可为空），驳回时当理由（必填）—— 一个输入框对应两种动作，符合客服操作直觉 */
const comment = ref('')

/** Agent 的结构化结果：run 或 result 为空说明 Agent 还没给出可审批的方案 */
const result = computed(() => props.ticket?.agent_run?.result ?? null)

// 每次打开都清空上一次的输入，否则"上次的备注"会悄悄跟着这次提交上去
watch(
  () => props.modelValue,
  (visible) => {
    if (visible) comment.value = ''
  },
)

function handleVisibleChange(value: boolean): void {
  emit('update:modelValue', value)
}

function handleApprove(): void {
  emit('approve', comment.value.trim())
}

function handleReject(): void {
  const reason = comment.value.trim()
  // 驳回必须留痕：后端会把 reason 写进工单事件，事后能追溯"为什么没退"
  if (!reason) {
    ElMessage.warning('驳回需要填写理由')
    return
  }
  emit('reject', reason)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="人工审批"
    width="680px"
    :close-on-click-modal="false"
    @update:model-value="handleVisibleChange"
  >
    <el-alert
      type="warning"
      show-icon
      :closable="false"
      title="Agent 已生成处理方案，请确认是否执行"
      description="批准会恢复被中断的 Agent 流程继续执行（例如真正发起退款）；驳回会结束本次 Agent 流程，工单转由人工处理。"
    />

    <el-descriptions class="approval__meta" :column="2" size="small" border>
      <el-descriptions-item label="工单号">
        <span class="approval__no">{{ ticket?.ticket_no ?? '—' }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="工单状态">
        <StatusTag v-if="ticket" :value="ticket.status" :map="TICKET_STATUS_MAP" />
        <span v-else>—</span>
      </el-descriptions-item>
      <el-descriptions-item label="问题类型">
        <StatusTag :value="result?.issue_type ?? ticket?.type ?? null" :map="TICKET_TYPE_MAP" />
      </el-descriptions-item>
      <el-descriptions-item label="业务动作">
        <span>{{ result?.business_action || '—' }}</span>
      </el-descriptions-item>
    </el-descriptions>

    <el-alert v-if="ticket?.agent_run?.review_payload?.reason"
      :title="ticket.agent_run.review_payload.reason" type="info" :closable="false" />
    <div v-if="ticket?.agent_run?.review_payload?.order_snapshot" class="approval__block">
      <div class="approval__label">本轮审核的订单与售后状态</div>
      <pre class="pre-wrap">{{ formatRefundReview(ticket.agent_run.review_payload.order_snapshot) }}</pre>
    </div>

    <template v-if="result">
      <div class="approval__block">
        <div class="approval__label">方案摘要</div>
        <div class="pre-wrap">{{ result.summary || '—' }}</div>
      </div>

      <div class="approval__block">
        <div class="approval__label">处理方案</div>
        <div class="pre-wrap">{{ result.solution || '—' }}</div>
      </div>
    </template>

    <!-- 没有 result 就不能批：批准要恢复图继续跑，前提是图已经生成了方案 -->
    <el-empty v-else description="该工单暂无 Agent 方案，无法审批" :image-size="60" />

    <div class="approval__block">
      <div class="approval__label">审批意见</div>
      <el-input
        v-model="comment"
        type="textarea"
        :rows="3"
        maxlength="500"
        show-word-limit
        placeholder="批准时可不填；驳回时必须填写理由（会记录到工单事件中）"
      />
    </div>

    <template #footer>
      <el-button :disabled="loading" @click="handleVisibleChange(false)">取消</el-button>
      <el-button type="danger" :loading="loading" :disabled="!ticket?.agent_run?.approval_available" @click="handleReject">驳回</el-button>
      <el-button type="primary" :loading="loading" :disabled="!ticket?.agent_run?.approval_available" @click="handleApprove">批准并执行</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.approval__meta {
  margin: 12px 0;
}

.approval__no {
  font-family: Consolas, Monaco, monospace;
}

.approval__block {
  margin-top: 12px;
}

.approval__label {
  font-weight: 600;
  margin-bottom: 4px;
}
</style>
