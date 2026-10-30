<script setup lang="ts">
/**
 * 工单详情（用户端最重要的页面）：对话 + Agent 实时进度 + Agent 处理结果。
 *
 * 这个页面要处理四件"容易做错"的事：
 *   1. 触发 Agent（POST /tickets/{id}/process）是**同步等待**的（要调 LLM，5~30 秒），
 *      所以必须先订阅 SSE 再发请求，否则整个过程都看不到；
 *   2. SSE 断开 ≠ Agent 失败：先查一次真实状态，还在 PROCESSING 才退避重连（最多 3 次），
 *      已经有结果就直接用查回来的结果；
 *   3. 刷新页面时如果工单还在 PROCESSING（Agent 正在跑），要自动重新连上进度流；
 *   4. 组件卸载时必须调用订阅返回的取消函数，否则会留下一个永不关闭的连接。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import AgentProcessTimeline from '@/components/agent/AgentProcessTimeline.vue'
import AgentResultCard from '@/components/agent/AgentResultCard.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useTicketReadReceipt } from '@/composables/useTicketReadReceipt'
import TicketImageUpload from '@/components/ticket/TicketImageUpload.vue'
import TicketMessageInput from '@/components/ticket/TicketMessageInput.vue'
import TicketMessageList from '@/components/ticket/TicketMessageList.vue'
import {
  closeTicket,
  getTicket,
  processTicket,
  requestHuman,
  reopenTicket,
  sendMessage,
  subscribeTicketStream,
  trackEventId,
} from '@/api/tickets'
import type { AgentEventInfo, AgentStreamEvent, TicketDetail } from '@/types'
import { TICKET_STATUS_MAP, TICKET_TYPE_MAP } from '@/utils/dict'
import { formatDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const ticketId = computed(() => Number(route.params.id))

const loading = ref(true)
const detail = ref<TicketDetail | null>(null)
useTicketReadReceipt(detail)
const sending = ref(false)
const processing = ref(false)
const actingAction = ref('')
let humanRefresh: ReturnType<typeof setInterval> | null = null
let humanRefreshing = false

const messageInputRef = ref<InstanceType<typeof TicketMessageInput> | null>(null)

// ======================================================================
// 详情
// ======================================================================
async function fetchDetail(): Promise<TicketDetail | null> {
  try {
    const data = await getTicket(ticketId.value)
    detail.value = data
    return data
  } catch {
    // 404/403 等由拦截器提示；不覆盖已有 detail，避免把一个能看的页面清空
    return null
  }
}

// ======================================================================
// SSE：Agent 业务进度
// ======================================================================
const MAX_RETRY = 3

/** SSE 当前是否连着（驱动时间线上的"实时"角标） */
const streaming = ref(false)
/** SSE 收到的事件（先展示这份，收到 done 后改用落库的 agent_events） */
const liveEvents = ref<AgentEventInfo[]>([])

let cancelStream: (() => void) | null = null
let retryCount = 0
let reconnectTimer: number | null = null
/** 是否已收到 done / error 帧（后端发完就会关流，正常关闭不该被当成断开） */
let streamFinished = false
/**
 * 已收到的最后一个事件序号（SSE 的 `id:`）。
 *
 * 重连时把它作为 `Last-Event-ID` 传回去，后端就只回放**没收到过**的那一段 ——
 * 断线期间 Agent 走的步骤不会丢，也不会和已经渲染出来的重复。
 * 不复位：它描述的是"这个浏览器已经看到哪儿了"，跨重连保持单调。
 */
let lastEventId: number | null = null

function stopStream(): void {
  cancelStream?.()
  cancelStream = null
  streaming.value = false
}

function clearReconnectTimer(): void {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

function startStream(): void {
  stopStream()
  clearReconnectTimer()

  // 首次连接清空本地事件；重连时保留（后端只补缺失的那一段，清掉反而会闪一下）
  if (lastEventId === null) liveEvents.value = []
  streamFinished = false
  streaming.value = true

  cancelStream = subscribeTicketStream(
    ticketId.value,
    {
      onEvent: (event) => handleStreamEvent(event),
      onError: () => {
        // fetch 层失败（网络/代理断流）
        streaming.value = false
        void handleStreamBroken()
      },
      onClose: () => {
        streaming.value = false
        // 正常结束（done/error 之后后端主动关流）不算断开
        if (!streamFinished) void handleStreamBroken()
      },
    },
    { lastEventId },
  )
}

function handleStreamEvent(event: AgentStreamEvent): void {
  lastEventId = trackEventId(event, lastEventId)

  if (event.type === 'event') {
    liveEvents.value.push({
      event_type: event.event_type ?? '',
      message: event.message,
      created_at: event.at ?? new Date().toISOString(),
    })
    return
  }

  if (event.type === 'done' || event.type === 'error') {
    // 结束信号：取消订阅 + 重新拉详情（真实结果以数据库为准）
    streamFinished = true
    stopStream()
    retryCount = 0
    if (event.type === 'error') {
      // 不额外弹 toast：POST /process 失败时拦截器已经弹过，避免同一件事提示两遍
      console.warn('[agent stream error]', event.message, event.detail)
    }
    void refreshAfterStream()
  }
}

/** 收到 done/error 之后：落库的 agent_events 才是权威数据（事件是先落库再推送的） */
async function refreshAfterStream(): Promise<void> {
  const fresh = await fetchDetail()
  if (fresh) liveEvents.value = []
  // 极少数情况下 done 之后状态还没落库：继续观察一会儿
  if (fresh?.status === 'PROCESSING') scheduleReconnect()
}

/**
 * 流断开了怎么办：**先查真实状态再决定**。
 * 断开可能只是网络抖动，Agent 在服务端可能已经跑完 —— 直接当失败处理会误导用户。
 */
async function handleStreamBroken(): Promise<void> {
  stopStream()
  const fresh = await fetchDetail()
  if (fresh?.status === 'PROCESSING') {
    scheduleReconnect()
  } else if (!fresh) {
    ElMessage.warning('进度连接已断开，请刷新页面查看最新进度')
  }
  // fresh 且状态不是 PROCESSING：说明已经有结果了，用查回来的详情直接展示即可
}

function scheduleReconnect(): void {
  if (retryCount >= MAX_RETRY) {
    ElMessage.warning('处理进度连接多次断开，请稍后刷新页面查看最新进度')
    return
  }
  retryCount += 1
  const delay = 1000 * 2 ** (retryCount - 1) // 1s → 2s → 4s 退避，别把后端打满
  ElMessage.warning(
    `进度连接中断，${delay / 1000} 秒后自动重连（第 ${retryCount}/${MAX_RETRY} 次）`,
  )
  clearReconnectTimer()
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null
    startStream()
  }, delay)
}

// ======================================================================
// 操作
// ======================================================================
async function handleProcess(): Promise<void> {
  if (processing.value) return // 禁止重复点击：后端每次点击都会新建一次 AgentRun
  processing.value = true
  retryCount = 0

  // 提交后再连流；持久化事件支持回放。

  try {
    await processTicket(ticketId.value)
    startStream()
  } catch {
    // Agent 执行失败后端返回 500，拦截器已提示；SSE 会推 error 帧并触发详情刷新
  } finally {
    processing.value = false
  }

  await fetchDetail()
}

async function handleRequestHuman(): Promise<void> {
  actingAction.value = 'human'
  try {
    detail.value = await requestHuman(ticketId.value)
    ElMessage.success(
      detail.value.handling_mode === 'HUMAN_PENDING'
        ? '已申请转人工，正在安全交接'
        : '已转人工，请等待客服回复',
    )
    if (detail.value.handling_mode === 'HUMAN_PENDING') startStream()
    else stopStream()
  } finally {
    actingAction.value = ''
  }
}

async function handleSend(content: string): Promise<void> {
  if (sending.value) return
  sending.value = true
  try {
    await sendMessage(ticketId.value, content)
    messageInputRef.value?.clear() // 只有发送成功才清空，失败时保留用户写的内容
    await fetchDetail()
  } catch {
    // 拦截器已提示（例如工单已关闭）
  } finally {
    sending.value = false
  }
}

async function handleReopen(): Promise<void> {
  if (actingAction.value) return
  try {
    await ElMessageBox.confirm(
      '重新打开后工单回到处理中，可以继续让 Agent 或客服处理。',
      '重新打开工单',
      {
        confirmButtonText: '重新打开',
        cancelButtonText: '取消',
        type: 'info',
      },
    )
  } catch {
    return
  }
  actingAction.value = 'reopen'
  try {
    detail.value = await reopenTicket(ticketId.value)
    ElMessage.success('工单已重新打开')
  } catch {
    /* 拦截器已提示 */
  } finally {
    actingAction.value = ''
  }
}

async function handleClose(): Promise<void> {
  if (actingAction.value) return
  try {
    await ElMessageBox.confirm('确认关闭工单？关闭后如果问题再次出现，可以重新打开。', '关闭工单', {
      confirmButtonText: '确认关闭',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  actingAction.value = 'close'
  try {
    detail.value = await closeTicket(ticketId.value)
    ElMessage.success('工单已关闭')
  } catch {
    /* 拦截器已提示 */
  } finally {
    actingAction.value = ''
  }
}

// ======================================================================
// 计算属性
// ======================================================================
const canReopen = computed(
  () => detail.value?.status === 'RESOLVED' || detail.value?.status === 'CLOSED',
)
const isClosed = computed(() => detail.value?.status === 'CLOSED')
const isWaitingHuman = computed(
  () => detail.value?.status === 'WAITING_HUMAN' && detail.value.handling_mode === 'AGENT',
)
const isManual = computed(() => detail.value?.handling_mode !== 'AGENT')
const agentRunning = computed(() => processing.value || detail.value?.agent_status === 'RUNNING')

const processButtonText = computed(() => {
  if (agentRunning.value) return '处理中…'
  return detail.value?.agent_run ? '让 Agent 处理' : '开始处理'
})

/**
 * 时间线的事件来源：
 *   有实时事件（刚跑完/正在跑）就用实时的；
 *   否则用详情里落库的 agent_events（刷新页面回看历史进度）。
 */
const timelineEvents = computed<AgentEventInfo[]>(() => {
  if (liveEvents.value.length > 0) return liveEvents.value
  return detail.value?.agent_events ?? []
})

onMounted(async () => {
  const fresh = await fetchDetail()
  loading.value = false
  // 刷新恢复：工单还是 PROCESSING 说明后台有 Agent 在跑，重新连上进度流
  if (fresh?.agent_status === 'RUNNING') startStream()
  humanRefresh = setInterval(async () => {
    if (
      !detail.value ||
      detail.value.handling_mode === 'AGENT' ||
      humanRefreshing ||
      isClosed.value
    )
      return
    humanRefreshing = true
    try {
      await fetchDetail()
    } finally {
      humanRefreshing = false
    }
  }, 5000)
})

onBeforeUnmount(() => {
  // 关键：组件卸载就断开 SSE，否则连接会一直挂着
  clearReconnectTimer()
  stopStream()
  if (humanRefresh !== null) clearInterval(humanRefresh)
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <div class="ticket-title">
        <el-button link @click="router.push({ name: 'ticket-list' })">
          <el-icon><ArrowLeft /></el-icon>
          <span>我的工单</span>
        </el-button>
        <h2 class="page-header__title">工单详情</h2>
      </div>
    </div>

    <div v-if="!loading && !detail" class="page-card">
      <el-result icon="warning" title="工单不存在或无权查看">
        <template #extra>
          <el-button type="primary" @click="router.push({ name: 'ticket-list' })"
            >返回我的工单</el-button
          >
        </template>
      </el-result>
    </div>

    <template v-if="detail">
      <!-- 顶部：工单信息 + 操作 -->
      <div class="page-card">
        <div class="ticket-head">
          <div class="ticket-head__left">
            <span class="ticket-head__no">{{ detail.ticket_no }}</span>
            <StatusTag :value="detail.status" :map="TICKET_STATUS_MAP" />
            <StatusTag v-if="detail.type" :value="detail.type" :map="TICKET_TYPE_MAP" />
            <span class="text-secondary">创建于 {{ formatDateTime(detail.created_at) }}</span>
          </div>

          <div class="ticket-head__right">
            <el-button
              v-if="detail.handling_mode === 'AGENT' && !isClosed"
              :loading="actingAction === 'human'"
              @click="handleRequestHuman"
              >转人工客服</el-button
            >
            <el-button v-if="canReopen" :loading="actingAction === 'reopen'" @click="handleReopen">
              重新打开
            </el-button>
            <el-button
              type="danger"
              plain
              :loading="actingAction === 'close'"
              :disabled="isClosed"
              @click="handleClose"
            >
              关闭工单
            </el-button>
          </div>
        </div>

        <!-- 待人工确认：这是本项目 HITL 的核心状态，必须让用户看得懂 -->
        <el-alert
          v-if="isManual"
          class="ticket-alert"
          type="info"
          show-icon
          :closable="false"
          :title="
            detail.handling_mode === 'HUMAN_PENDING'
              ? '正在安全转接人工客服'
              : detail.assignee_id
                ? '人工客服正在跟进'
                : '已进入人工客服待办队列'
          "
          description="请在下方继续发送消息。转接完成后由客服回复，Agent 不会自动处理；已完成的业务操作不会被撤销。"
        />
        <el-alert
          v-if="isWaitingHuman"
          class="ticket-alert"
          type="warning"
          show-icon
          :closable="false"
          title="Agent 已将处理方案提交人工审核，客服确认后会自动执行"
          description="审核通过后系统会自动执行处理方案（例如退款），你无需重复提交。"
        />

        <div class="ticket-content">
          <div class="ticket-content__label">问题描述</div>
          <div class="pre-wrap">{{ detail.content }}</div>
        </div>
      </div>

      <div class="ticket-grid">
        <!-- 左：对话 -->
        <div class="ticket-chat">
          <div class="page-card">
            <h3 class="page-card__title">对话记录</h3>
            <TicketMessageList :messages="detail.messages" />
            <TicketMessageInput ref="messageInputRef" :loading="sending" @send="handleSend" />
            <TicketImageUpload
              :ticket-id="ticketId"
              :disabled="detail.status === 'CLOSED'"
              @uploaded="fetchDetail()"
            />
          </div>
        </div>

        <!-- 右：Agent 进度 + 结果 -->
        <div class="ticket-side">
          <div class="page-card">
            <AgentProcessTimeline
              :events="timelineEvents"
              :status="detail.status"
              :live="streaming"
            />

            <div class="ticket-process">
              <el-button
                type="primary"
                :loading="processing"
                :disabled="agentRunning || isManual"
                @click="handleProcess"
              >
                {{ processButtonText }}
              </el-button>
              <span class="text-secondary ticket-process__hint">
                {{
                  isManual
                    ? '当前由人工跟进，负责客服交回后才能重新启动 Agent。'
                    : 'Agent 会查询业务数据和客服规则，给出处理方案。'
                }}
              </span>
            </div>
          </div>

          <AgentResultCard :run="detail.agent_run" />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ticket-title {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ticket-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.ticket-head__left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.ticket-head__no {
  font-family: var(--font-mono);
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.3px;
}

.ticket-head__right {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ticket-head__right .el-button + .el-button {
  margin-left: 0;
}

.ticket-alert {
  margin-bottom: 12px;
}

.ticket-content {
  padding-top: 14px;
  border-top: 1px solid var(--border-color);
}

.ticket-content__label {
  font-weight: 600;
  margin-bottom: 4px;
}

.ticket-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(320px, 1fr);
  gap: 16px;
  align-items: start;
}

.ticket-chat,
.ticket-side {
  min-width: 0;
}

.ticket-side {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ticket-side .page-card + .page-card {
  margin-top: 0;
}

.ticket-process {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border-color);
}

.ticket-process .el-button {
  width: 100%;
}

.ticket-process__hint {
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 1100px) {
  .ticket-grid {
    grid-template-columns: 1fr;
  }
}
</style>
