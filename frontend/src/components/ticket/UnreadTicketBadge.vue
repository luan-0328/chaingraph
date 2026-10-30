<script setup lang="ts">
/**
 * 未读工单消息入口：轮询未读数并显示角标。
 * 视觉上是一个轻量图标按钮，和顶部导航的其他操作保持一致。
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getUnreadSummary } from '@/api/tickets'

const props = defineProps<{ to: string }>()
const router = useRouter()
const count = ref(0)
let timer: ReturnType<typeof setInterval> | null = null
let refreshing = false
async function refresh(): Promise<void> {
  if (document.visibilityState !== 'visible' || refreshing) return
  refreshing = true
  try {
    count.value = (await getUnreadSummary()).unread_messages
  } catch {
    /* 网络恢复后重试 */
  } finally {
    refreshing = false
  }
}
onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    void refresh()
  }, 10000)
  window.addEventListener('ticket-read', refresh)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('ticket-read', refresh)
})
</script>

<template>
  <el-badge :value="count" :hidden="count === 0" :max="99" class="msg-badge">
    <button type="button" class="msg-btn" aria-label="工单消息" @click="router.push(props.to)">
      <el-icon><ChatDotRound /></el-icon>
      <span class="msg-btn__label">消息</span>
    </button>
  </el-badge>
</template>

<style scoped>
.msg-badge {
  display: inline-flex;
}

.msg-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding: 0 12px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-regular);
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
  transition:
    background-color var(--dur) var(--ease),
    color var(--dur) var(--ease);
}

.msg-btn:hover {
  background: #f4f6fb;
  color: var(--text-primary);
}

.msg-btn .el-icon {
  font-size: 18px;
}

@media (max-width: 960px) {
  .msg-btn__label {
    display: none;
  }
}
</style>
