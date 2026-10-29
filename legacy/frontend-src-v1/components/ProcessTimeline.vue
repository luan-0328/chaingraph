<script setup lang="ts">
/**
 * 处理进度时间线（前端文档第二十九节）。
 *
 * 数据来源有两个，合起来渲染：
 *   1. **本次会话的 SSE 事件** —— 处理过程中实时追加（"正在查询订单信息"…）；
 *   2. **工单状态** —— 刷新页面后事件没了，就按状态推导出一个合理的进度。
 *
 * 展示的是业务步骤，不是 LLM 的思考过程。
 */
import { computed } from 'vue'

import type { AgentProgressEvent } from '@/types/agent'
import type { TicketStatus } from '@/types/ticket'
import { formatDateTime, formatTime } from '@/utils/format'

const props = defineProps<{
  status: TicketStatus
  events: AgentProgressEvent[]
  createdAt: string
  updatedAt: string
}>()

type TimelineType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

interface Step {
  label: string
  time: string
  type: TimelineType
  hollow: boolean
}

const steps = computed<Step[]>(() => {
  const list: Step[] = []

  // ① 工单创建
  list.push({
    label: '工单创建',
    time: formatDateTime(props.createdAt),
    type: 'success',
    hollow: false,
  })

  // ② 本次会话的实时进度（去掉重复的连续文案）
  const seen: string[] = []
  for (const event of props.events) {
    if (event.type !== 'progress') continue
    if (seen[seen.length - 1] === event.message) continue
    seen.push(event.message)
    list.push({
      label: event.message,
      time: formatTime(event.at),
      type: event.stage === 'executing' ? 'primary' : 'primary',
      hollow: false,
    })
  }

  // ③ 没有实时事件时，按状态给出一个概括步骤（刷新页面后的场景）
  if (seen.length === 0 && props.status !== 'OPEN') {
    list.push({
      label: props.status === 'PROCESSING' ? 'Agent 正在处理' : 'Agent 已完成分析',
      time: props.status === 'PROCESSING' ? '' : formatDateTime(props.updatedAt),
      type: 'primary',
      hollow: false,
    })
  }

  // ④ 收尾步骤：由工单状态决定
  if (props.status === 'PROCESSING') {
    list.push({ label: '处理中…', time: '', type: 'primary', hollow: true })
  } else if (props.status === 'WAITING_HUMAN') {
    list.push({ label: '等待人工确认', time: '', type: 'warning', hollow: true })
  } else if (props.status === 'CLOSED' || props.status === 'RESOLVED') {
    list.push({
      label: props.status === 'CLOSED' ? '处理完成' : '已解决',
      time: formatDateTime(props.updatedAt),
      type: 'success',
      hollow: false,
    })
  }

  return list
})
</script>

<template>
  <div class="page-card">
    <h3 class="page-card__title">处理进度</h3>

    <el-timeline>
      <el-timeline-item
        v-for="(step, index) in steps"
        :key="index"
        :type="step.type"
        :hollow="step.hollow"
        :timestamp="step.time"
        placement="top"
      >
        {{ step.label }}
      </el-timeline-item>
    </el-timeline>
  </div>
</template>
