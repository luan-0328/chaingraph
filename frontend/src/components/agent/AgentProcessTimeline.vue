<script setup lang="ts">
/**
 * Agent 业务事件时间线。
 *
 * 只展示**业务事件**（v2.md §43）：开始处理 / 查询订单 / 检索规则 / 执行退款 …
 * 绝不展示模型的思维链（CoT）或原始 prompt —— 那是内部实现，对用户既没用也不安全。
 * 事件的 event_type 是后端枚举，中文步骤名一律走 agentEventMeta()。
 */
import { computed } from 'vue'

import type { AgentEventInfo, TicketStatus } from '@/types'
import { TICKET_STATUS_MAP, agentEventMeta } from '@/utils/dict'
import { formatTime } from '@/utils/format'
import StatusTag from '@/components/common/StatusTag.vue'

const props = withDefaults(
  defineProps<{
    events: AgentEventInfo[]
    /** 工单当前状态（展示在标题右侧，方便对照进度） */
    status?: TicketStatus
    /** 是否来自 SSE 实时推送（true 时显示"实时"角标） */
    live?: boolean
  }>(),
  { status: undefined, live: false },
)

const hasEvents = computed(() => props.events.length > 0)
</script>

<template>
  <div class="agent-timeline">
    <div class="agent-timeline__header">
      <span class="agent-timeline__title">Agent 处理进度</span>
      <el-tag v-if="live" type="primary" size="small" effect="dark" disable-transitions
        >实时</el-tag
      >
      <StatusTag v-if="status" :value="status" :map="TICKET_STATUS_MAP" />
    </div>

    <el-empty v-if="!hasEvents" description="暂无处理进度" :image-size="60" />

    <el-timeline v-else>
      <el-timeline-item
        v-for="(event, index) in events"
        :key="`${event.event_type}-${event.created_at}-${index}`"
        :type="agentEventMeta(event.event_type).tag"
        :timestamp="formatTime(event.created_at)"
        placement="top"
      >
        <div class="agent-timeline__step">{{ agentEventMeta(event.event_type).label }}</div>
        <div class="agent-timeline__message">{{ event.message }}</div>
      </el-timeline-item>
    </el-timeline>
  </div>
</template>

<style scoped>
.agent-timeline__header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
}

.agent-timeline__title {
  font-size: 15px;
  font-weight: 600;
}

.agent-timeline__step {
  font-weight: 600;
  font-size: 13.5px;
}

.agent-timeline__message {
  color: var(--text-regular);
  font-size: 12.5px;
  margin-top: 2px;
  line-height: 1.6;
}

/* 每个事件做成一张浅色小卡：事件多的时候比裸文字更容易扫读 */
.agent-timeline :deep(.el-timeline-item__content) {
  padding: 10px 12px;
  border: 1px solid #eef1f7;
  border-radius: var(--radius-md);
  background: #fbfcfe;
}

.agent-timeline :deep(.el-timeline-item__timestamp) {
  margin-bottom: 4px;
}

.agent-timeline :deep(.el-timeline-item:last-child) {
  padding-bottom: 0;
}
</style>
