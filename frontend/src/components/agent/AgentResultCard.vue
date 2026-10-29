<script setup lang="ts">
/**
 * Agent 处理结果卡：把一次 AgentRun 的结构化结果摊开展示。
 *
 * 展示的是**落库的结构化结果**（AgentDecision + AgentResult），不是模型原文：
 *   决策摘要 / 分析 / 处理方案 / 参考知识 / 工具调用 / 业务动作 / 是否需要人工。
 * 这里同样不展示思维链。
 */
import { computed } from 'vue'

import type { AgentRunInfo } from '@/types'
import { TICKET_TYPE_MAP, ticketTypeMeta } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'
import StatusTag from '@/components/common/StatusTag.vue'

const props = defineProps<{ run: AgentRunInfo | null }>()

const decision = computed(() => props.run?.decision ?? null)
const result = computed(() => props.run?.result ?? null)

/** 决策摘要：优先用决策里的（更早产出），旧数据可能只有 result.summary */
const summaryText = computed(() => decision.value?.summary || result.value?.summary || '')

const references = computed(() => result.value?.references ?? [])
const toolResults = computed(() => result.value?.tool_results ?? [])

/** 是否要人工：decision / result / run 三处任一为真都算（历史数据可能只落了一处） */
const needHuman = computed(
  () =>
    Boolean(props.run?.need_human) ||
    Boolean(result.value?.need_human) ||
    Boolean(decision.value?.need_human),
)

const issueType = computed(() => decision.value?.issue_type ?? result.value?.issue_type ?? null)

/**
 * 业务动作文案。
 * business_action 的取值被后端 Literal 约束为 apply_refund / cancel_order / ""，
 * dict.ts 里没有对应的字典项，所以就地渲染；
 * "已执行"还是"待执行"由 need_human 决定（需人工时只是提交了申请，钱还没动）。
 */
const businessActionText = computed(() => {
  const action = result.value?.business_action ?? ''
  if (!action) return ''
  if (action === 'apply_refund') return needHuman.value ? '待执行退款（等待人工审核）' : '已执行退款'
  if (action === 'cancel_order')
    return needHuman.value ? '待执行取消订单（等待人工审核）' : '已执行取消订单'
  return action // 后端新增了动作就直接原样显示，不猜语义
})

const businessActionTag = computed(() => {
  const action = result.value?.business_action ?? ''
  if (!action) return 'info'
  return needHuman.value ? 'warning' : 'success'
})
</script>

<template>
  <div class="page-card agent-result">
    <h3 class="page-card__title">Agent 处理结果</h3>

    <el-empty v-if="!run" description="暂无 Agent 处理记录" :image-size="80" />

    <template v-else>
      <el-alert
        v-if="needHuman"
        class="agent-result__alert"
        type="warning"
        show-icon
        :closable="false"
        title="已提交人工审核"
        description="客服确认后系统会自动执行处理方案，你也可以在工单对话里继续补充说明。"
      />

      <el-alert
        v-if="run.error"
        class="agent-result__alert"
        type="error"
        show-icon
        :closable="false"
        title="本次 Agent 执行出错"
        :description="run.error"
      />

      <el-descriptions :column="2" border size="small" class="agent-result__meta">
        <el-descriptions-item label="执行编号">{{ run.agent_run_id }}</el-descriptions-item>
        <el-descriptions-item label="问题类型">
          <StatusTag v-if="issueType" :value="issueType" :map="TICKET_TYPE_MAP" />
          <span v-else>—</span>
        </el-descriptions-item>
        <el-descriptions-item label="开始时间">
          {{ formatDateTime(run.started_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="结束时间">
          {{ formatDateTime(run.finished_at) }}
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="summaryText" class="agent-result__block">
        <div class="agent-result__label">
          决策摘要
          <span v-if="issueType" class="text-secondary agent-result__label-note">
            （判定为：{{ ticketTypeMeta(issueType).label }}）
          </span>
        </div>
        <div class="pre-wrap">{{ summaryText }}</div>
      </div>

      <div v-if="result?.analysis" class="agent-result__block">
        <div class="agent-result__label">分析</div>
        <div class="pre-wrap">{{ result.analysis }}</div>
      </div>

      <div v-if="result?.solution" class="agent-result__block">
        <div class="agent-result__label">处理方案</div>
        <div class="pre-wrap">{{ result.solution }}</div>
      </div>

      <div class="agent-result__block">
        <div class="agent-result__label">参考知识</div>
        <ul v-if="references.length > 0" class="agent-result__refs">
          <li v-for="(reference, index) in references" :key="index">
            {{ reference.title || '未命名知识' }}
            <span class="text-secondary">— 来源：{{ reference.source || '—' }}</span>
          </li>
        </ul>
        <div v-else class="text-secondary">本次未检索知识库</div>
      </div>

      <div class="agent-result__block">
        <div class="agent-result__label">执行过的工具</div>
        <div v-if="toolResults.length > 0" class="agent-result__tools">
          <el-tag
            v-for="(tool, index) in toolResults"
            :key="index"
            :type="tool.ok === false ? 'danger' : 'success'"
            size="small"
            disable-transitions
          >
            {{ tool.tool || '未知工具' }} · {{ tool.ok === false ? '失败' : '成功' }}
          </el-tag>
        </div>
        <div v-else class="text-secondary">本次未调用业务工具</div>
      </div>

      <div class="agent-result__block">
        <div class="agent-result__label">业务动作</div>
        <el-tag v-if="businessActionText" :type="businessActionTag" disable-transitions>
          {{ businessActionText }}
        </el-tag>
        <span v-else class="text-secondary">无需执行系统动作</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.agent-result__alert {
  margin-bottom: 12px;
}

.agent-result__meta {
  margin-bottom: 16px;
}

.agent-result__block {
  margin-bottom: 14px;
}

.agent-result__label {
  font-weight: 600;
  margin-bottom: 4px;
}

.agent-result__label-note {
  font-weight: 400;
  font-size: 12px;
}

.agent-result__refs {
  margin: 0;
  padding-left: 18px;
}

.agent-result__tools {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
