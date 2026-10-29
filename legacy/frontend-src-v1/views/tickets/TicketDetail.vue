<script setup lang="ts">
/**
 * 工单详情 —— 整个前端最重要的页面（前端文档第二十节）。
 *
 * 它把几件事串起来：
 *   1. 展示工单信息（TicketInfoCard）
 *   2. 展示处理进度（ProcessTimeline）—— 处理中由 SSE 实时驱动
 *   3. 展示 Agent 结果（AgentResultCard）
 *   4. 触发 Agent 处理（POST /process）+ 防止重复点击
 *   5. 需要人工确认时提供审批入口（ApprovalDialog）
 *   6. SSE 断线恢复 / 页面刷新恢复
 *
 * 具体逻辑都在子组件里，这个文件只做"编排"。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  approveTicket,
  getTicket,
  processTicket,
  rejectTicket,
  subscribeTicketStream,
} from '@/api/ticket'
import AgentResultCard from '@/components/AgentResultCard.vue'
import ApprovalDialog from '@/components/ApprovalDialog.vue'
import ProcessTimeline from '@/components/ProcessTimeline.vue'
import TicketInfoCard from '@/components/TicketInfoCard.vue'
import TicketStatusTag from '@/components/TicketStatusTag.vue'
import type { AgentProgressEvent } from '@/types/agent'
import type { TicketDetail } from '@/types/ticket'

const route = useRoute()
const router = useRouter()

const ticketId = computed(() => Number(route.params.id))

const loading = ref(true)
const loadError = ref('')
const detail = ref<TicketDetail | null>(null)

const processing = ref(false)
const events = ref<AgentProgressEvent[]>([])
const toolsUsed = ref<string[]>([])

const approvalVisible = ref(false)
const approvalLoading = ref(false)

/** SSE 连接状态，仅用于界面提示与调试 */
const streamState = ref<'idle' | 'connecting' | 'open' | 'closed' | 'error'>('idle')

/** 当前 SSE 的取消函数（组件卸载 / 处理结束时调用） */
let closeStream: (() => void) | null = null

const canProcess = computed(() => {
  if (!detail.value || processing.value) return false
  // 待人工确认时应该去审批，而不是重新跑 Agent
  return detail.value.status !== 'PROCESSING' && detail.value.status !== 'WAITING_HUMAN'
})

const processButtonText = computed(() => {
  if (processing.value) return '处理中…'
  return detail.value?.agent_result ? '重新处理' : '开始处理'
})

// ======================================================================
// 加载详情
// ======================================================================
async function loadDetail(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    detail.value = await getTicket(ticketId.value)
  } catch (error) {
    detail.value = null
    loadError.value = '工单加载失败或不存在'
    if (import.meta.env.DEV) console.error(error)
  } finally {
    loading.value = false
  }
}

// ======================================================================
// Agent 处理
// ======================================================================
async function startProcess(): Promise<void> {
  if (!detail.value || processing.value) return

  processing.value = true
  events.value = []
  toolsUsed.value = []

  // 先建立 SSE，再触发处理：
  // 后端在建流时会回放历史事件，所以即使晚连上也不会漏掉进度。
  openStream()

  try {
    const result = await processTicket(ticketId.value)
    toolsUsed.value = result.tools_used ?? []
    ElMessage.success('Agent 处理完成')
    await loadDetail()
  } catch {
    // 500 等错误已由拦截器提示，同时 SSE 也会推 error 事件
    await loadDetail()
  } finally {
    processing.value = false
    closeStream?.()
    closeStream = null
    streamState.value = 'idle'
  }
}

// ======================================================================
// SSE：订阅进度 + 断线恢复
// ======================================================================
function openStream(retry = 0): void {
  closeStream?.()
  streamState.value = 'connecting'

  closeStream = subscribeTicketStream(ticketId.value, {
    onEvent(event) {
      streamState.value = 'open'

      if (event.type === 'progress') {
        events.value.push(event)
        return
      }

      if (event.type === 'done') {
        events.value.push(event)
        if (event.tools_used?.length) toolsUsed.value = event.tools_used
        // 处理结束：刷新工单，拿到最终状态和结果
        void loadDetail()
        return
      }

      if (event.type === 'error') {
        events.value.push(event)
        ElMessage.error(event.message || 'Agent 处理失败，请稍后重试')
        void loadDetail()
      }
    },

    onError(error) {
      streamState.value = 'error'
      // 断线 ≠ 失败（前端文档第三十节）：
      // 先查一次工单状态，再决定要不要重连
      void recoverFromDisconnect(retry, error)
    },

    onClose() {
      streamState.value = 'closed'
    },
  })
}

async function recoverFromDisconnect(retry: number, error: Error): Promise<void> {
  if (import.meta.env.DEV) console.warn('[SSE] 连接中断', error.message)

  await loadDetail()

  if (detail.value?.status === 'PROCESSING' && retry < 3) {
    // 还在处理中：退避重连，历史事件会被后端补发
    const delay = 1500 * (retry + 1)
    ElMessage.warning(`进度连接中断，${delay / 1000} 秒后自动重连…`)
    window.setTimeout(() => openStream(retry + 1), delay)
    return
  }

  if (detail.value?.status === 'PROCESSING') {
    ElMessage.warning('进度连接已中断，请刷新页面查看最新状态')
  }
  // 其它状态说明已经处理完了，loadDetail 已经把结果取回来了，不需要重连
}

// ======================================================================
// 人工审批（HITL）
// ======================================================================
async function handleApprove(comment: string): Promise<void> {
  approvalLoading.value = true
  try {
    detail.value = await approveTicket(ticketId.value, comment)
    approvalVisible.value = false
    ElMessage.success('已确认处理')
  } catch {
    // 409（状态不对）等错误由拦截器提示
  } finally {
    approvalLoading.value = false
  }
}

async function handleReject(comment: string): Promise<void> {
  approvalLoading.value = true
  try {
    detail.value = await rejectTicket(ticketId.value, comment)
    approvalVisible.value = false
    ElMessage.success('已驳回')
  } catch {
    // 同上
  } finally {
    approvalLoading.value = false
  }
}

// ======================================================================
// 生命周期
// ======================================================================
onMounted(async () => {
  await loadDetail()

  // 刷新恢复（前端文档第三十一节）：
  // 如果工单还在处理中，说明有 Agent 任务在跑，重新把进度流接上
  if (detail.value?.status === 'PROCESSING') {
    processing.value = true
    openStream()
  }
})

onBeforeUnmount(() => {
  closeStream?.()
  closeStream = null
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">
        工单详情
        <span v-if="detail" class="text-secondary detail-id">#{{ detail.ticket_id }}</span>
      </h2>
      <el-button @click="router.push({ name: 'ticket-list' })">返回列表</el-button>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="page-card">
      <el-skeleton :rows="6" animated />
    </div>

    <!-- 加载失败 -->
    <el-result
      v-else-if="loadError"
      icon="warning"
      :title="loadError"
      sub-title="请确认工单编号是否正确，或稍后重试"
    >
      <template #extra>
        <el-button type="primary" @click="loadDetail">重新加载</el-button>
        <el-button @click="router.push({ name: 'ticket-list' })">返回列表</el-button>
      </template>
    </el-result>

    <!-- 正常内容 -->
    <template v-else-if="detail">
      <el-row :gutter="16">
        <el-col :xs="24" :lg="14">
          <TicketInfoCard :detail="detail" />
        </el-col>
        <el-col :xs="24" :lg="10">
          <ProcessTimeline
            :status="detail.status"
            :events="events"
            :created-at="detail.created_at"
            :updated-at="detail.updated_at"
          />
        </el-col>
      </el-row>

      <AgentResultCard :detail="detail" :tools-used="toolsUsed" />

      <!-- 需要人工确认 -->
      <div v-if="detail.status === 'WAITING_HUMAN'" class="page-card approval-card">
        <h3 class="page-card__title">需要人工确认</h3>
        <p class="text-regular approval-card__text">
          Agent 已生成处理方案（详见上方处理结果），请确认是否执行。
        </p>
        <el-button type="primary" @click="approvalVisible = true">去确认</el-button>
      </div>

      <!-- 底部操作栏 -->
      <div class="page-card action-bar">
        <div class="action-bar__left">
          <span class="text-secondary">当前状态：</span>
          <TicketStatusTag :status="detail.status" />
          <span v-if="processing" class="text-secondary action-bar__hint">
            Agent 正在处理，请稍候…
          </span>
        </div>

        <el-button
          type="primary"
          :loading="processing"
          :disabled="!canProcess"
          @click="startProcess"
        >
          {{ processButtonText }}
        </el-button>
      </div>
    </template>

    <ApprovalDialog
      v-model="approvalVisible"
      :detail="detail"
      :loading="approvalLoading"
      @approve="handleApprove"
      @reject="handleReject"
    />
  </div>
</template>

<style scoped>
.detail-id {
  font-size: 14px;
  font-weight: 400;
  margin-left: 6px;
}

.approval-card__text {
  margin: 0 0 12px;
}

.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
  position: sticky;
  bottom: 0;
}

.action-bar__left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.action-bar__hint {
  margin-left: 8px;
}
</style>
