<script setup lang="ts">
/**
 * 客服工单详情 —— 客服的主战场（v2.md §23.2、§26、§43、§47）。
 *
 * 三列布局对应客服的三件事：
 *   左：这笔工单是谁的、关联哪笔订单/售后（先搞清楚事实）；
 *   中：和用户对话（客服说的话会写到工单消息里，用户端能看到）；
 *   右：Agent 做了什么、给了什么方案（业务事件 + 结果，不展示模型思维链）。
 *
 * HITL（人工审批）有两条路，本页默认走**工单维度**：
 *   · approveTicket(ticketId, comment) —— 恢复被 interrupt 中断的 LangGraph 图继续执行，
 *     后续节点会真正发起退款；图跑完工单状态由后端决定。适合"Agent 已经规划好、只差人工点确认"。
 *   · approveRefund(orderId, comment)  —— 直接执行业务退款（退款 + 售后完成 + 订单 REFUNDED），
 *     不碰 Agent 流程。适合"Agent 没参与、客服自己判断该退"的场景（本页放在订单侧入口）。
 * 两者业务结果相似，但一个是"恢复图"、一个是"直接动业务"，混用会出现重复退款的边界问题，
 * 所以同一个工单只走一条路。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import AgentProcessTimeline from '@/components/agent/AgentProcessTimeline.vue'
import AgentResultCard from '@/components/agent/AgentResultCard.vue'
import ApprovalDialog from '@/components/agent/ApprovalDialog.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useTicketReadReceipt } from '@/composables/useTicketReadReceipt'
import TicketImageUpload from '@/components/ticket/TicketImageUpload.vue'
import TicketMessageInput from '@/components/ticket/TicketMessageInput.vue'
import TicketMessageList from '@/components/ticket/TicketMessageList.vue'
import {
  assignTicket,
  approveTicket,
  closeTicket,
  getOrder,
  getTicket,
  rejectTicket,
  replyTicket,
  resolveTicket,
  takeoverTicket,
  returnToAgent,
} from '@/api/customerService'
import { processTicket, subscribeTicketStream, trackEventId } from '@/api/tickets'
import { useAuthStore } from '@/stores/auth'
import type { AgentEventInfo, AgentStreamEvent, OrderDetail, TicketDetail } from '@/types'
import {
  FULFILLMENT_STATUS_MAP,
  PAYMENT_STATUS_MAP,
  TICKET_PRIORITY_MAP,
  TICKET_STATUS_MAP,
  TICKET_TYPE_MAP,
} from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const ticketId = computed(() => Number(route.params.id))

const loading = ref(false)
const detail = ref<TicketDetail | null>(null)
useTicketReadReceipt(detail)
/** 关联订单：工单只存 order_id，金额/履约状态要单独查一次（客服看单时常要核这几个数） */
const order = ref<OrderDetail | null>(null)

const acting = ref(false) // 领取/审批/解决/关闭这类"改状态"的操作
const sending = ref(false) // 回复消息
const processing = ref(false) // 重新让 Agent 处理（同步等 LLM，可能十几秒）
const approvalVisible = ref(false)
let reviewRefresh: ReturnType<typeof setInterval> | null = null
let reviewRefreshing = false

function stopReviewRefresh(): void {
  if (reviewRefresh !== null) clearInterval(reviewRefresh)
  reviewRefresh = null
}

watch(
  () => `${detail.value?.status}:${detail.value?.handling_mode}`,
  () => {
    stopReviewRefresh()
    if (!isWaitingHuman.value) approvalVisible.value = false
    if (detail.value?.status === 'WAITING_HUMAN' || detail.value?.handling_mode !== 'AGENT') {
      reviewRefresh = setInterval(async () => {
        if (reviewRefreshing) return
        reviewRefreshing = true
        try {
          await load(true)
        } finally {
          reviewRefreshing = false
        }
      }, 5000)
    }
  },
)

// ---------------- SSE：Agent 实时进度 ----------------
const streaming = ref(false)
/** SSE 收到的实时事件：只用于"正在跑"的过程展示，服务端持久化的事件才是最终依据 */
const liveEvents = ref<AgentEventInfo[]>([])
let unsubscribe: (() => void) | null = null
/**
 * 已收到的最后一个事件序号（SSE 的 `id:`）。
 * 重连时作为 `Last-Event-ID` 传回，后端只补"没收到过"的那一段（见 api/tickets.ts）。
 */
let lastEventId: number | null = null

function eventKey(event: AgentEventInfo): string {
  return `${event.event_type}|${event.message}|${event.created_at}`
}

function pushLiveEvent(event: AgentEventInfo): void {
  const key = eventKey(event)
  // 后端建流时会回放历史事件，同一件事可能收两次，按"类型+文案+时间"去重
  if (liveEvents.value.some((item) => eventKey(item) === key)) return
  liveEvents.value.push(event)
}

/** 时间线数据 = 服务端持久化事件 + 实时事件（去掉实时里已经落库的那部分） */
const timelineEvents = computed<AgentEventInfo[]>(() => {
  const persisted = detail.value?.agent_events ?? []
  if (liveEvents.value.length === 0) return persisted
  const seen = new Set(persisted.map(eventKey))
  return [...persisted, ...liveEvents.value.filter((item) => !seen.has(eventKey(item)))]
})

function stopStream(): void {
  if (unsubscribe) {
    unsubscribe()
    unsubscribe = null
  }
  streaming.value = false
}

function startStream(): void {
  stopStream()
  streaming.value = true
  if (lastEventId === null) liveEvents.value = []
  unsubscribe = subscribeTicketStream(
    ticketId.value,
    {
      onEvent: (event: AgentStreamEvent) => {
        lastEventId = trackEventId(event, lastEventId)

        // done / error 是流的终态：先记一条事件，再拉一次详情把落库的 AgentRun 与工单状态同步回来
        if (event.type === 'done' || event.type === 'error') {
          pushLiveEvent({
            event_type: event.type === 'done' ? 'COMPLETED' : 'FAILED',
            message: event.message,
            created_at: event.at ?? new Date().toISOString(),
          })
          stopStream()
          void load(true)
          return
        }
        pushLiveEvent({
          event_type: event.event_type ?? 'EVENT',
          message: event.message,
          created_at: event.at ?? new Date().toISOString(),
        })
      },
      onError: () => {
        // SSE 断了不影响页面：详情本身还能刷新，别弹错误吓客服（连接失败由后端日志追踪）
        streaming.value = false
      },
      onClose: () => {
        streaming.value = false
      },
    },
    { lastEventId },
  )
}

// ---------------- 数据加载 ----------------
async function loadOrder(orderId: number): Promise<void> {
  try {
    order.value = await getOrder(orderId)
  } catch {
    // 订单可能已被删除/无权限：只影响"关联订单"卡片，工单主体照常显示
    order.value = null
  }
}

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  try {
    const data = await getTicket(ticketId.value)
    detail.value = data
    if (data.order_id) await loadOrder(data.order_id)
    else order.value = null
  } catch {
    // 错误提示由 request.ts 拦截器统一处理
  } finally {
    if (!silent) loading.value = false
  }
}

// ---------------- 顶部操作 ----------------
const status = computed(() => detail.value?.status ?? null)
const isWaitingHuman = computed(
  () => status.value === 'WAITING_HUMAN' && detail.value?.handling_mode === 'AGENT',
)
const isClosed = computed(() => status.value === 'CLOSED')
/** 已领取的工单不再显示"领取"（避免抢同事的单）；真要转派由后端传 assignee_id */
const canAssign = computed(() => Boolean(detail.value) && !detail.value?.assignee_id)

function goBack(): void {
  void router.push({ name: 'cs-ticket-list' })
}

function goOrder(): void {
  // 订单列表页支持 keyword 搜订单号；查不到订单详情时退化成用 order_id 也搜不出结果，所以优先用单号
  const keyword = order.value?.order_no ?? String(detail.value?.order_id ?? '')
  void router.push({ name: 'cs-order-list', query: { keyword } })
}

function goAfterSale(): void {
  void router.push({
    name: 'cs-after-sale-list',
    query: { order_id: String(detail.value?.order_id ?? '') },
  })
}

/**
 * 领取工单：必须显式把自己 user_id 传进去。
 * 领取 = 显式传自己的 user_id（不传 assignee_id 时后端会填当前用户；传 null 才是取消领取）。
 */
async function handleAssign(): Promise<void> {
  acting.value = true
  try {
    detail.value = await assignTicket(ticketId.value, auth.user?.user_id)
    ElMessage.success('已领取该工单')
  } catch {
    // 例如已被他人领取（409）
  } finally {
    acting.value = false
  }
}

async function handleTakeover(): Promise<void> {
  try {
    await ElMessageBox.confirm('接管后 Agent 将停止恢复当前方案，后续由你人工跟进。', '人工接管', {
      type: 'warning',
    })
  } catch {
    return
  }
  acting.value = true
  try {
    detail.value = await takeoverTicket(ticketId.value)
    stopStream()
    ElMessage.success('已接管，当前 Agent 方案已停止')
  } finally {
    acting.value = false
  }
}

async function handleReturnToAgent(): Promise<void> {
  acting.value = true
  try {
    detail.value = await returnToAgent(ticketId.value)
    ElMessage.success('已明确交回 Agent，可发起新的自动处理')
  } finally {
    acting.value = false
  }
}

async function handleApprove(comment: string): Promise<void> {
  acting.value = true
  try {
    const run = detail.value?.agent_run
    if (!run) return
    const updated = await approveTicket(
      ticketId.value,
      run.agent_run_id,
      run.review_round,
      comment || undefined,
    )
    detail.value = updated
    approvalVisible.value = false
    ElMessage.success('批准已提交，等待后台执行')
    if (updated.order_id) await loadOrder(updated.order_id)
    // 批准后图继续往下跑（可能要几秒），此时还能看实时进度
    if (updated.status === 'PROCESSING') startStream()
  } catch {
    approvalVisible.value = false
    await load(true)
  } finally {
    acting.value = false
  }
}

async function handleReject(reason: string): Promise<void> {
  acting.value = true
  try {
    const run = detail.value?.agent_run
    if (!run) return
    detail.value = await rejectTicket(ticketId.value, run.agent_run_id, run.review_round, reason)
    startStream()
    approvalVisible.value = false
    ElMessage.success('驳回已提交，等待后台完成交接')
  } catch {
    approvalVisible.value = false
    await load(true)
  } finally {
    acting.value = false
  }
}

async function handleResolve(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      '请填写解决方案（会记录到工单并通知用户）',
      '标记已解决',
      {
        confirmButtonText: '确认解决',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '例如：已为用户办理退款，预计 3 个工作日内到账',
        inputValidator: (text: string) => (text.trim() ? true : '解决方案不能为空'),
      },
    )
    acting.value = true
    detail.value = await resolveTicket(ticketId.value, value)
    ElMessage.success('已标记为已解决')
  } catch {
    // 用户点了取消，或接口失败（提示已由拦截器弹出）
  } finally {
    acting.value = false
  }
}

async function handleClose(): Promise<void> {
  try {
    await ElMessageBox.confirm('关闭后用户不能再在该工单继续沟通，确认关闭吗？', '关闭工单', {
      confirmButtonText: '确认关闭',
      cancelButtonText: '取消',
      type: 'warning',
    })
    acting.value = true
    detail.value = await closeTicket(ticketId.value)
    ElMessage.success('工单已关闭')
  } catch {
    // 取消或失败
  } finally {
    acting.value = false
  }
}

/**
 * 重新让 Agent 处理：会新建一次 AgentRun（历史记录保留，v2.md §66）。
 * 处理期间订阅 SSE，客服能看到 Agent 正在做哪一步。
 *
 * ⚠️ 后端 POST /tickets/{id}/process 目前按"工单归属"校验（get_ticket_raw(user_id=当前登录人)），
 * 所以客服对**别人提交的**工单点这个按钮可能收到 403；这属于后端权限口径问题，
 * 前端如实把错误交给拦截器展示。
 */
async function handleProcess(): Promise<void> {
  processing.value = true
  // 先持久化任务，再订阅其事件，避免收到上一轮任务的终态。
  try {
    await processTicket(ticketId.value)
    startStream()
    await load(true)
    ElMessage.success('Agent 任务已提交，正在等待处理')
  } catch {
    // 失败信息由拦截器统一弹出
  } finally {
    processing.value = false
    // 已经不在 PROCESSING 就别白占着连接
    if (status.value !== 'PROCESSING') stopStream()
  }
}

async function handleSend(content: string): Promise<void> {
  sending.value = true
  try {
    await replyTicket(ticketId.value, content)
    // 重新拉详情（而不是把返回值 push 进数组）：消息排序/状态变化都以服务端为准
    await load(true)
  } catch {
    // 失败提示由拦截器处理
  } finally {
    sending.value = false
  }
}

// ---------------- 历史 Agent 执行 ----------------
/** 耗时（秒）：只有 started_at 与 finished_at 都有才有意义，否则返回占位符 */
function durationOf(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt || !finishedAt) return '—'
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime()
  if (Number.isNaN(ms) || ms < 0) return '—'
  return `${(ms / 1000).toFixed(1)}s`
}

onMounted(async () => {
  await load()
  // 挂载时正在处理中的工单：自动订阅进度，客服不用手动刷新
  if (detail.value?.agent_status === 'RUNNING') startStream()
})

// 同一路由不同 id（从列表连续点进去）时组件不会重建，要自己重新加载
watch(ticketId, () => {
  stopStream()
  liveEvents.value = []
  void load()
})

onUnmounted(() => {
  stopStream()
  stopReviewRefresh()
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <div class="ticket__head">
        <el-button text @click="goBack">
          <el-icon><ArrowLeft /></el-icon>
          <span>返回列表</span>
        </el-button>
        <h2 class="page-header__title">{{ detail?.ticket_no ?? '工单详情' }}</h2>
        <StatusTag v-if="detail" :value="detail.status" :map="TICKET_STATUS_MAP" />
      </div>

      <div v-if="detail" class="ticket__actions">
        <el-button
          v-if="isWaitingHuman"
          type="warning"
          :disabled="acting || !detail.agent_run?.approval_available"
          @click="approvalVisible = true"
        >
          人工审批
        </el-button>
        <el-button
          v-if="canAssign"
          :loading="acting"
          :disabled="detail.handling_mode === 'HUMAN_PENDING'"
          @click="handleAssign"
          >领取工单</el-button
        >
        <el-button
          v-if="
            detail.handling_mode === 'HUMAN' &&
            detail.assignee_id === auth.user?.user_id &&
            !isClosed
          "
          :disabled="acting"
          @click="handleReturnToAgent"
          >交回 Agent</el-button
        >
        <el-button v-if="isWaitingHuman" :disabled="acting" @click="handleTakeover"
          >人工接管</el-button
        >
        <el-button v-if="!isClosed" :disabled="acting" @click="handleResolve">标记已解决</el-button>
        <el-button v-if="!isClosed" :disabled="acting" @click="handleClose">关闭工单</el-button>
        <el-button
          v-if="!isClosed"
          type="primary"
          :loading="processing"
          :disabled="processing || detail.handling_mode !== 'AGENT'"
          @click="handleProcess"
        >
          重新让 Agent 处理
        </el-button>
      </div>
    </div>

    <template v-if="detail">
      <el-alert
        v-if="detail.handling_mode !== 'AGENT'"
        type="info"
        :closable="false"
        show-icon
        :title="
          detail.handling_mode === 'HUMAN_PENDING'
            ? 'Agent 正在安全交接，请等待确认后人工操作'
            : '人工处理模式：领取后可在原工单中与用户交流并处理业务'
        "
        :description="detail.handoff_reason || '由负责客服明确交回后才允许重新启动 Agent。'"
      />
      <el-alert
        v-if="isWaitingHuman"
        :type="detail.agent_run?.approval_blocked_reason ? 'error' : 'warning'"
        :closable="false"
        show-icon
        :title="
          detail.agent_run?.approval_blocked_reason ||
          `待人工审批，截止时间：${formatDateTime(detail.agent_run?.review_deadline_at)}`
        "
        description="请核对方案后处理。超时将转人工跟进，不会自动批准或退款。"
      />
      <div class="ticket-layout">
        <!-- 左：工单信息 + 关联单据 -->
        <div class="ticket-col ticket-col--meta">
          <div class="page-card">
            <h3 class="page-card__title">工单信息</h3>
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="工单号">{{ detail.ticket_no }}</el-descriptions-item>
              <el-descriptions-item label="状态">
                <StatusTag :value="detail.status" :map="TICKET_STATUS_MAP" />
              </el-descriptions-item>
              <el-descriptions-item label="问题类型">
                <StatusTag :value="detail.type" :map="TICKET_TYPE_MAP" />
              </el-descriptions-item>
              <el-descriptions-item label="优先级">
                <StatusTag :value="detail.priority" :map="TICKET_PRIORITY_MAP" />
              </el-descriptions-item>
              <el-descriptions-item label="提交用户">
                <!-- 后端只给了 user_id；listUsers 的 keyword 只匹配 username/nickname，
                     拿 id 查不到人，所以这里如实显示 id（要显示昵称需要后端补 /users/{id}） -->
                <span class="text-secondary">用户 #{{ detail.user_id }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="领取人">
                <span v-if="detail.assignee_id" class="text-secondary">
                  客服 #{{ detail.assignee_id }}
                </span>
                <span v-else class="text-secondary">未领取</span>
              </el-descriptions-item>
              <el-descriptions-item label="提交时间">
                {{ formatDateTime(detail.created_at) }}
              </el-descriptions-item>
              <el-descriptions-item label="更新时间">
                {{ formatDateTime(detail.updated_at) }}
              </el-descriptions-item>
            </el-descriptions>

            <div class="ticket__content">
              <div class="ticket__label">问题描述</div>
              <div class="pre-wrap">{{ detail.content }}</div>
            </div>
          </div>

          <div v-if="detail.order_id || detail.after_sale_id" class="page-card">
            <h3 class="page-card__title">关联单据</h3>
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item v-if="detail.order_id" label="订单">
                {{ order?.order_no ?? `#${detail.order_id}` }}
              </el-descriptions-item>
              <el-descriptions-item v-if="detail.after_sale_id" label="售后单">
                #{{ detail.after_sale_id }}
              </el-descriptions-item>
              <template v-if="order">
                <el-descriptions-item label="订单金额">
                  <span class="text-price">{{ formatAmount(order.total_amount) }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="支付状态">
                  <StatusTag :value="order.payment_status" :map="PAYMENT_STATUS_MAP" />
                </el-descriptions-item>
                <el-descriptions-item label="履约状态">
                  <StatusTag :value="order.fulfillment_status" :map="FULFILLMENT_STATUS_MAP" />
                </el-descriptions-item>
              </template>
            </el-descriptions>

            <div class="ticket__links">
              <el-button v-if="detail.order_id" size="small" @click="goOrder">查看订单</el-button>
              <el-button v-if="detail.after_sale_id" size="small" @click="goAfterSale">
                查看售后
              </el-button>
            </div>
          </div>
        </div>

        <!-- 中：与用户的对话 -->
        <div class="ticket-col ticket-col--chat">
          <div class="page-card">
            <h3 class="page-card__title">沟通记录</h3>
            <TicketMessageList :messages="detail.messages" perspective="staff" />
            <TicketMessageInput :loading="sending" @send="handleSend" />
            <TicketImageUpload
              :ticket-id="ticketId"
              :disabled="
                detail.status === 'CLOSED' ||
                detail.handling_mode === 'HUMAN_PENDING' ||
                detail.assignee_id !== auth.user?.user_id
              "
              @uploaded="load(true)"
            />
          </div>
        </div>

        <!-- 右：Agent 过程 / 方案 / 历史执行 -->
        <div class="ticket-col ticket-col--agent">
          <div class="page-card">
            <h3 class="page-card__title">
              Agent 处理过程
              <el-tag v-if="streaming" type="warning" size="small" disable-transitions>
                实时进度中
              </el-tag>
            </h3>
            <AgentProcessTimeline
              :events="timelineEvents"
              :status="detail.status"
              :live="streaming"
            />
          </div>

          <!-- AgentResultCard 自带 page-card 外壳，这里不要再包一层，否则会出现双卡片 -->
          <AgentResultCard :run="detail.agent_run" />

          <div class="page-card">
            <h3 class="page-card__title">历史 Agent 执行</h3>
            <el-table v-if="detail.agent_runs.length" :data="detail.agent_runs" size="small">
              <el-table-column prop="agent_run_id" label="#" width="60" />
              <el-table-column label="状态" width="130">
                <template #default="{ row }">
                  <!-- AgentRunStatus 没有对应的中文字典（dict.ts 只覆盖工单/订单/售后枚举），
                       这里展示原始状态码，不自己造映射 -->
                  <el-tag size="small" disable-transitions>{{ row.status }}</el-tag>
                  <!-- 正在跑的时候顺带显示"卡在哪个节点"，排查时最省事 -->
                  <div v-if="row.current_node" class="text-secondary" style="font-size: 12px">
                    {{ row.current_node }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="重试" width="90">
                <template #default="{ row }">
                  <!-- 重试次数与"是否断点续跑"是排查 Agent 稳定性最直接的两个数 -->
                  <el-tag
                    v-if="row.retry_count > 0"
                    type="warning"
                    size="small"
                    disable-transitions
                  >
                    {{ row.retry_count }} 次重试
                  </el-tag>
                  <span v-else class="text-secondary">—</span>
                </template>
              </el-table-column>
              <el-table-column label="断点续跑" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.resumed" type="success" size="small" disable-transitions
                    >是</el-tag
                  >
                  <span v-else class="text-secondary">—</span>
                </template>
              </el-table-column>
              <el-table-column label="RAG" width="70">
                <template #default="{ row }">
                  <span :class="row.used_rag ? '' : 'text-secondary'">
                    {{ row.used_rag ? '是' : '否' }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="工具" width="70">
                <template #default="{ row }">
                  <span :class="row.used_tool ? '' : 'text-secondary'">
                    {{ row.used_tool ? '是' : '否' }}
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="耗时" width="80">
                <template #default="{ row }">
                  {{ durationOf(row.started_at, row.finished_at) }}
                </template>
              </el-table-column>
              <el-table-column label="失败原因" min-width="150">
                <template #default="{ row }">
                  <!-- 只看"状态=FAILED"没法定位问题，把 error 摆出来（截断 + 悬浮看全文） -->
                  <el-tooltip v-if="row.error" :content="row.error" placement="top">
                    <span class="text-secondary">{{ row.error.slice(0, 30) }}…</span>
                  </el-tooltip>
                  <span v-else class="text-secondary">—</span>
                </template>
              </el-table-column>
              <el-table-column label="开始时间" min-width="160">
                <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
              </el-table-column>
              <template #empty>
                <el-empty description="该工单还没有 Agent 执行记录" :image-size="60" />
              </template>
            </el-table>
            <el-empty v-else description="该工单还没有 Agent 执行记录" :image-size="60" />
          </div>
        </div>
      </div>
    </template>

    <div v-else-if="!loading" class="page-card">
      <el-empty description="工单不存在或无权查看" />
    </div>

    <ApprovalDialog
      v-model="approvalVisible"
      :ticket="detail"
      :loading="acting"
      @approve="handleApprove"
      @reject="handleReject"
    />
  </div>
</template>

<style scoped>
.ticket__head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ticket__head .page-header__title {
  font-family: var(--font-mono);
  font-size: 20px;
  letter-spacing: -0.3px;
}

.ticket__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ticket__actions .el-button + .el-button {
  margin-left: 0;
}

.ticket__content {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border-color);
}

.ticket__label {
  font-weight: 600;
  margin-bottom: 4px;
}

.ticket__links {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}

.ticket-layout {
  display: grid;
  grid-template-columns: minmax(220px, 0.8fr) minmax(0, 1.55fr) minmax(300px, 1fr);
  gap: 16px;
  align-items: start;
}

.ticket-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.ticket-col .page-card + .page-card {
  margin-top: 0;
}

.ticket-col--chat .chat {
  max-height: 520px;
}

/* 时间线标题后的"实时进度中"标签 */
.page-card__title .el-tag {
  margin-left: 8px;
  font-weight: 400;
}

@media (max-width: 1360px) {
  .ticket-layout {
    grid-template-columns: 240px minmax(0, 1fr);
  }

  .ticket-col--agent {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    align-items: start;
  }
}

@media (max-width: 900px) {
  .ticket-layout {
    grid-template-columns: 1fr;
  }

  .ticket-col--agent {
    grid-column: auto;
    grid-template-columns: 1fr;
  }

  .ticket__head {
    flex-wrap: wrap;
  }
}
</style>
