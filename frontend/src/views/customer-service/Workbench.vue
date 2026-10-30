<script setup lang="ts">
/**
 * 客服工作台首页：数据概览 + 待人工确认工单（v2.md §23.1）。
 *
 * 设计取舍：概览页只回答"今天有没有活要干"，所以放的是**可点击的待办数字**，
 * 不放趋势图 —— 客服需要的是入口，不是报表。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { RouteLocationRaw } from 'vue-router'

import { getDashboard, listTickets } from '@/api/customerService'
import StatusTag from '@/components/common/StatusTag.vue'
import type { DashboardStats, TicketSummary } from '@/types'
import { TICKET_PRIORITY_MAP, TICKET_STATUS_MAP, TICKET_TYPE_MAP } from '@/utils/dict'
import { formatDateTime, truncate } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const stats = ref<DashboardStats | null>(null)
const pendingTickets = ref<TicketSummary[]>([])

/**
 * 待人工确认数：后端给的是"状态 → 数量"字典，没有这个 key 就说明一张都没有。
 * 项目开启了 noUncheckedIndexedAccess，索引访问本身就是 number | undefined，所以必须 ?? 0。
 */
const waitingHuman = computed(() => stats.value?.ticket_by_status.WAITING_HUMAN ?? 0)

interface StatCard {
  key: string
  label: string
  value: number
  /** 图标 + 强调色：让 6 个数字在一眼扫过时能区分开 */
  icon: string
  tone: 'blue' | 'amber' | 'green' | 'violet' | 'rose' | 'cyan'
  /** 需要重点跟进的数字标红，避免满屏黑字看不出轻重 */
  danger?: boolean
  /** 带 to 的卡片可点击（点"待人工确认"直接跳到筛好的工单列表） */
  to?: RouteLocationRaw
}

const cards = computed<StatCard[]>(() => {
  const data = stats.value
  return [
    {
      key: 'ticket_total',
      label: '工单总数',
      value: data?.ticket_total ?? 0,
      icon: 'Tickets',
      tone: 'blue',
    },
    {
      key: 'waiting_human',
      label: '待人工确认',
      value: waitingHuman.value,
      icon: 'WarningFilled',
      tone: 'amber',
      danger: waitingHuman.value > 0,
      to: { name: 'cs-ticket-list', query: { waiting_human: '1' } },
    },
    {
      key: 'order_total',
      label: '订单总数',
      value: data?.order_total ?? 0,
      icon: 'List',
      tone: 'cyan',
    },
    {
      key: 'order_paid',
      label: '已支付订单',
      value: data?.order_paid ?? 0,
      icon: 'Money',
      tone: 'green',
    },
    {
      key: 'after_sale_pending',
      label: '待审售后',
      value: data?.after_sale_pending ?? 0,
      icon: 'RefreshLeft',
      tone: 'violet',
    },
    {
      key: 'product_on_sale',
      label: '上架商品',
      value: data?.product_on_sale ?? 0,
      icon: 'Goods',
      tone: 'rose',
    },
  ]
})

/** Agent 运行分布：键是 AgentRunStatus（PENDING/PROCESSING/WAITING_HUMAN/COMPLETED/FAILED/REJECTED） */
const agentRunStats = computed(() => Object.entries(stats.value?.agent_run_by_status ?? {}))

function onCardClick(card: StatCard): void {
  if (card.to) void router.push(card.to)
}

function goPending(): void {
  void router.push({ name: 'cs-ticket-list', query: { waiting_human: '1' } })
}

function goDetail(ticketId: number): void {
  void router.push({ name: 'cs-ticket-detail', params: { id: ticketId } })
}

/** 整行可点：客服看列表时顺手就点进去了，不用非得瞄准"处理"按钮 */
function onRowClick(row: TicketSummary): void {
  goDetail(row.ticket_id)
}

let refreshing = false
let refreshTimer: ReturnType<typeof setInterval> | null = null

async function load(silent = false): Promise<void> {
  if (refreshing) return
  refreshing = true
  if (!silent) loading.value = true
  try {
    const [dashboard, tickets] = await Promise.all([
      getDashboard(),
      // 概览只要前 5 条待人工确认的：多了页面就变成第二个列表页了
      listTickets({ waiting_human: true, limit: 5 }),
    ])
    stats.value = dashboard
    pendingTickets.value = tickets.items
  } catch {
    // 失败信息由 request.ts 拦截器统一弹出，这里只需要保证页面不崩
  } finally {
    loading.value = false
    refreshing = false
  }
}

onMounted(() => {
  void load()
  refreshTimer = setInterval(() => {
    void load(true)
  }, 15000)
})
onUnmounted(() => {
  if (refreshTimer !== null) clearInterval(refreshTimer)
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-header__title">数据概览</h2>
        <p class="page-header__sub">工单、订单与 Agent 运行情况，每 15 秒自动刷新一次。</p>
      </div>
      <el-button @click="load()">
        <el-icon><Refresh /></el-icon>
        <span>刷新</span>
      </el-button>
    </div>

    <!-- 有待办时把最重要的一件事提到最上面，客服不用自己找 -->
    <button v-if="waitingHuman > 0" type="button" class="pending-banner" @click="goPending">
      <span class="pending-banner__icon">
        <el-icon><WarningFilled /></el-icon>
      </span>
      <span class="pending-banner__text">
        <strong>有 {{ waitingHuman }} 张工单等待人工确认</strong>
        <small>Agent 已生成处理方案，审核通过后会自动执行退款等业务动作。</small>
      </span>
      <span class="pending-banner__cta">
        去处理
        <el-icon><ArrowRight /></el-icon>
      </span>
    </button>

    <div class="stat-grid">
      <button
        v-for="card in cards"
        :key="card.key"
        type="button"
        class="stat-card"
        :class="[`stat-card--${card.tone}`, { 'stat-card--clickable': card.to }]"
        @click="onCardClick(card)"
      >
        <span class="stat-card__icon">
          <el-icon><component :is="card.icon" /></el-icon>
        </span>
        <span class="stat-card__body">
          <span class="stat-card__label">{{ card.label }}</span>
          <span class="stat-card__value" :class="{ 'stat-card__value--danger': card.danger }">
            {{ card.value }}
          </span>
        </span>
        <el-icon v-if="card.to" class="stat-card__arrow"><ArrowRight /></el-icon>
      </button>
    </div>

    <div class="page-card">
      <h3 class="page-card__title">Agent 运行状态分布</h3>
      <div v-if="agentRunStats.length" class="agent-stats">
        <!-- 后端 status 用原始码（如 COMPLETED / WAITING_HUMAN）：dict.ts 没有该枚举的字典，
             这里不自己造映射，直接展示原值，避免两处口径不一致 -->
        <div v-for="[status, count] in agentRunStats" :key="status" class="agent-stat">
          <span class="agent-stat__dot" :class="`agent-stat__dot--${status.toLowerCase()}`"></span>
          <span class="agent-stat__code">{{ status }}</span>
          <strong class="agent-stat__count">{{ count }}</strong>
        </div>
      </div>
      <el-empty v-else description="暂无 Agent 运行记录" :image-size="60" />
    </div>

    <div class="page-card">
      <div class="page-card__title-row">
        <h3 class="page-card__title">待处理工单（待人工确认）</h3>
        <el-button v-if="pendingTickets.length" link type="primary" @click="goPending">
          查看全部
          <el-icon><ArrowRight /></el-icon>
        </el-button>
      </div>
      <el-table
        v-if="pendingTickets.length"
        :data="pendingTickets"
        size="default"
        @row-click="onRowClick"
      >
        <el-table-column prop="ticket_no" label="工单号" width="180">
          <template #default="{ row }">
            <span class="mono">{{ row.ticket_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            {{ truncate(row.title || row.content, 30) }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.type" :map="TICKET_TYPE_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <StatusTag :value="row.priority" :map="TICKET_PRIORITY_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="TICKET_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column prop="user_id" label="提交人" width="90">
          <template #default="{ row }">
            <span class="text-secondary">#{{ row.user_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="goDetail(row.ticket_id)">处理</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无待人工确认的工单" />
    </div>
  </div>
</template>

<style scoped>
.pending-banner {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  padding: 16px 20px;
  margin-bottom: 18px;
  border: 1px solid #fbe3bc;
  border-radius: var(--radius-lg);
  background: #fffaf1;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease);
}

.pending-banner:hover {
  border-color: #f5c877;
  box-shadow: 0 8px 20px -12px rgb(240 160 32 / 45%);
}

.pending-banner__icon {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: var(--radius-md);
  background: #fef0d6;
  color: #c0801a;
  font-size: 19px;
  flex: none;
}

.pending-banner__text {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}

.pending-banner__text strong {
  font-size: 14px;
  font-weight: 650;
  color: #7a4c05;
}

.pending-banner__text small {
  margin-top: 2px;
  color: #a97b30;
  font-size: 12px;
}

.pending-banner__cta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #a86a0a;
  font-size: 12.5px;
  font-weight: 600;
  white-space: nowrap;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 20px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  background: #fff;
  box-shadow: var(--shadow-xs);
  font-family: inherit;
  text-align: left;
  cursor: default;
  transition:
    border-color var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease),
    transform var(--dur) var(--ease);
}

.stat-card--clickable {
  cursor: pointer;
}

.stat-card--clickable:hover {
  border-color: #d5def5;
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

.stat-card__icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: var(--radius-md);
  font-size: 19px;
  flex: none;
}

.stat-card--blue .stat-card__icon {
  background: #eef3ff;
  color: var(--brand);
}

.stat-card--amber .stat-card__icon {
  background: #fef6e9;
  color: #d18b12;
}

.stat-card--green .stat-card__icon {
  background: #e8f7f1;
  color: var(--success);
}

.stat-card--violet .stat-card__icon {
  background: #f1edff;
  color: #7357d9;
}

.stat-card--rose .stat-card__icon {
  background: #fef0f0;
  color: #d9534f;
}

.stat-card--cyan .stat-card__icon {
  background: #e9f7fb;
  color: #0f93b8;
}

.stat-card__body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.stat-card__label {
  color: var(--text-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.stat-card__value {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.3;
  letter-spacing: -0.6px;
  color: var(--text-primary);
}

.stat-card__value--danger {
  color: var(--price);
}

.stat-card__arrow {
  color: #c0c6d2;
  font-size: 13px;
}

.agent-stats {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 10px;
}

.agent-stat {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: #fbfcfe;
}

.agent-stat__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-secondary);
  flex: none;
}

.agent-stat__dot--completed {
  background: var(--success);
}

.agent-stat__dot--processing {
  background: var(--brand);
}

.agent-stat__dot--waiting_human {
  background: var(--warning);
}

.agent-stat__dot--failed {
  background: var(--danger);
}

.agent-stat__dot--pending {
  background: #a9b0bf;
}

.agent-stat__dot--rejected {
  background: #7357d9;
}

.agent-stat__code {
  color: var(--text-regular);
  font-family: var(--font-mono);
  font-size: 11.5px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-stat__count {
  color: var(--text-primary);
  font-size: 15px;
  font-weight: 650;
}

.page-card__title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-card__title-row .page-card__title {
  margin-bottom: 14px;
}

:deep(.el-table__row) {
  cursor: pointer;
}

@media (max-width: 640px) {
  .pending-banner {
    align-items: flex-start;
  }

  .pending-banner__cta {
    display: none;
  }

  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
}

@media (max-width: 560px) {
  .stat-grid {
    grid-template-columns: 1fr;
  }
}
</style>
