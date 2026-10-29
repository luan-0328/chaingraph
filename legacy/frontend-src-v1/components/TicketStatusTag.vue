<script setup lang="ts">
/**
 * 状态标签：把后端状态枚举统一渲染成带颜色的 Tag。
 * 前端文档第十七节要求把它封装成一个组件，避免各页面重复写样式。
 */
import { computed } from 'vue'

import type { TicketStatus } from '@/types/ticket'
import { TICKET_STATUS_MAP } from '@/utils/dict'

const props = defineProps<{ status: TicketStatus }>()

const meta = computed(
  () => TICKET_STATUS_MAP[props.status] ?? { label: props.status, tag: 'info' as const },
)
</script>

<template>
  <el-tag :type="meta.tag" disable-transitions>{{ meta.label }}</el-tag>
</template>
