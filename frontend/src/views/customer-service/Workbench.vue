<script setup lang="ts">
/**
 * 客服工作台首页：数据概览 + 待人工确认工单（v2.md §23.1）。
 *
 * 设计取舍：概览页只回答"今天有没有活要干"，所以放的是**可点击的待办数字**，
 * 不放趋势图 —— 客服需要的是入口，不是报表。
 */
import { computed, onMounted, ref } from 'vue'
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
  /** 需要重点跟进的数字标红，避免满屏黑字看不出轻重 */
  danger?: boolean
  /** 带 to 的卡片可点击（点"待人工确认"直接跳到筛好的工单列表） */
  to?: RouteLocationRaw
}

const cards = computed<StatCard[]>(() => {
  const data = stats.value
  return [
    { key: 'ticket_total', label: '工单总数', value: data?.ticket_total ?? 0 },
    {
      key: 'waiting_human',
      label: '待人工确认',
      value: waitingHuman.value,
      danger: waitingHuman.value > 0,
      to: { name: 'cs-ticket-list', query: { waiting_human: '1' } },
    },
    { key: 'order_total', label: '订单总数', value: data?.order_total ?? 0 },
    { key: 'order_paid', label: '已支付订单', value: data?.order_paid ?? 0 },
    { key: 'after_sale_pending', label: '待审售后', value: data?.after_sale_pending ?? 0 },
    { key: 'product_on_sale', label: '上架商品', value: data?.product_on_sale ?? 0 },
  ]
})

/** Agent 运行分布：键是 AgentRunStatus（PENDING/PROCESSING/WAITING_HUMAN/COMPLETED/FAILED/REJECTED） */
const agentRunStats = computed(() => Object.entries(stats.value?.agent_run_by_status ?? {}))

function onCardClick(card: StatCard): void {
  if (card.to) void router.push(card.to)
}

function goDetail(ticketId: number): void {
  void router.push({ name: 'cs-ticket-detail', params: { id: ticketId } })
}

/** 整行可点：客服看列表时顺手就点进去了，不用非得瞄准"处理"按钮 */
function onRowClick(row: TicketSummary): void {
  goDetail(row.ticket_id)
}

async function load(): Promise<void> {
  loading.value = true
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
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2 class="page-header__title">数据概览</h2>
      <el-button text :icon="'Refresh'" @click="load">刷新</el-button>
    </div>

    <div class="stat-grid">
      <div
        v-for="card in cards"
        :key="card.key"
        class="page-card stat-card"
        :class="{ 'stat-card--clickable': card.to }"
        @click="onCardClick(card)"
      >
        <div class="stat-card__label">{{ card.label }}</div>
        <div class="stat-card__value" :class="{ 'stat-card__value--danger': card.danger }">
          {{ card.value }}
        </div>
      </div>
    </div>

    <div class="page-card">
      <h3 class="page-card__title">Agent 运行状态分布</h3>
      <div v-if="agentRunStats.length" class="agent-stats">
        <!-- 后端 status 用原始码（如 COMPLETED / WAITING_HUMAN）：dict.ts 没有该枚举的字典，
             这里不自己造映射，直接展示原值，避免两处口径不一致 -->
        <el-tag v-for="[status, count] in agentRunStats" :key="status" disable-transitions>
          {{ status }}：{{ count }}
        </el-tag>
      </div>
      <el-empty v-else description="暂无 Agent 运行记录" :image-size="60" />
    </div>

    <div class="page-card">
      <h3 class="page-card__title">待处理工单（待人工确认）</h3>
      <el-table
        v-if="pendingTickets.length"
        :data="pendingTickets"
        size="default"
        @row-click="onRowClick"
      >
        <el-table-column prop="ticket_no" label="工单号" width="180" />
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
        <el-table-column prop="user_id" label="提交人" width="90" />
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
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.stat-card {
  cursor: default;
}

.stat-card--clickable {
  cursor: pointer;
  transition: border-color 0.2s;
}

.stat-card--clickable:hover {
  border-color: var(--brand);
}

.stat-card__label {
  color: var(--text-secondary);
  font-size: 13px;
}

.stat-card__value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.4;
}

.stat-card__value--danger {
  color: var(--price);
}

.agent-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
