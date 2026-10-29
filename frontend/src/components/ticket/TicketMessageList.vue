<script setup lang="ts">
/**
 * 工单对话列表：渲染 TicketMessageInfo[]，支持 用户 / 客服 / Agent / 系统 四种来源。
 *
 * 布局约定（复用 style.css 的 .chat* 类）：
 *   Agent  → 左侧气泡（带 Agent 标签，只展示业务内容，绝不展示模型思维链）
 *   系统   → 居中的小字提示（状态变更留痕，不是"谁说的话"）
 *
 * "谁的话靠右"取决于视角：
 *   perspective='user' （默认，用户端）→ USER 消息靠右
 *   perspective='staff'（客服端）    → CUSTOMER_SERVICE 消息靠右
 */
import { nextTick, ref, watch } from 'vue'

import type { TicketMessageInfo } from '@/types'
import { SENDER_LABEL } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    messages: TicketMessageInfo[]
    loading?: boolean
    /** 视角：决定哪一方的消息靠右显示 */
    perspective?: 'user' | 'staff'
  }>(),
  { loading: false, perspective: 'user' },
)

/** 左右气泡只是视觉区分，来源文案统一来自字典 SENDER_LABEL */
function isMine(message: TicketMessageInfo): boolean {
  const mineSender = props.perspective === 'staff' ? 'CUSTOMER_SERVICE' : 'USER'
  return message.sender_type === mineSender
}

function isSystem(message: TicketMessageInfo): boolean {
  return message.sender_type === 'SYSTEM'
}

function senderTagType(message: TicketMessageInfo): 'primary' | 'success' | 'warning' {
  if (message.sender_type === 'AGENT') return 'primary'
  if (message.sender_type === 'CUSTOMER_SERVICE') return 'success'
  return 'warning'
}

// 新消息进来后自动滚到底部：用户在工单里发言/Agent 推进时，最新内容应该可见
const listRef = ref<HTMLElement | null>(null)

watch(
  () => props.messages.length,
  async () => {
    await nextTick()
    const el = listRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<template>
  <div ref="listRef" v-loading="loading" class="chat">
    <el-empty v-if="messages.length === 0 && !loading" description="还没有对话记录" :image-size="60" />

    <div
      v-for="message in messages"
      :key="message.message_id"
      class="chat__item"
      :class="{ 'chat__item--right': isMine(message), 'chat__item--system': isSystem(message) }"
    >
      <div class="chat__meta">
        <template v-if="!isSystem(message)">
          <el-tag :type="senderTagType(message)" size="small" disable-transitions>
            {{ SENDER_LABEL[message.sender_type] }}
          </el-tag>
        </template>
        <span class="chat__time">{{ formatDateTime(message.created_at) }}</span>
      </div>

      <div class="chat__bubble">{{ message.content }}</div>
    </div>
  </div>
</template>

<style scoped>
.chat__time {
  margin-left: 6px;
}

/* 系统消息：居中、弱化，读起来像"日志"而不是"某个人的发言" */
.chat__item--system {
  text-align: center;
}

.chat__item--system .chat__bubble {
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  max-width: 100%;
}
</style>
