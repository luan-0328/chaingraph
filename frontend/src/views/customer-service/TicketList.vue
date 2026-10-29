<script setup lang="ts">
/**
 * 客服工单列表（v2.md §23.1、§45）：筛选 + 分页 + 领取。
 *
 * 客服每天第一件事就是"筛出我该处理的"，所以筛选条件（状态/只看我的/未领取/待人工确认）
 * 全部走服务端参数，不在前端过滤 —— 分页时前端过滤会得出错误的总数。
 */
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { assignTicket, listTickets } from '@/api/customerService'
import StatusTag from '@/components/common/StatusTag.vue'
import { useAuthStore } from '@/stores/auth'
import type { TicketSummary } from '@/types'
import { TICKET_STATUS_MAP, TICKET_STATUS_OPTIONS, TICKET_TYPE_MAP } from '@/utils/dict'
import { formatDateTime, truncate } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const loading = ref(false)
const rows = ref<TicketSummary[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

/** 正在领取的工单 id：让按钮只在这一行转圈，其他行还能点 */
const assigningId = ref<number | null>(null)

const filters = reactive({
  status: '',
  only_mine: false,
  unassigned: false,
  // 工作台点"待人工确认"跳过来时带 ?waiting_human=1，这里读一次当初始筛选
  waiting_human: route.query.waiting_human === '1',
  keyword: '',
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listTickets({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      // 空字符串代表"不限"，但空串会被后端当成一个真实的状态值，所以统一转成 undefined
      status: filters.status || undefined,
      only_mine: filters.only_mine || undefined,
      unassigned: filters.unassigned || undefined,
      waiting_human: filters.waiting_human || undefined,
      keyword: filters.keyword.trim() || undefined,
    })
    rows.value = result.items
    total.value = result.total
  } catch {
    // 错误提示由 request.ts 拦截器统一处理，这里保持列表可用即可
  } finally {
    loading.value = false
  }
}

function search(): void {
  page.value = 1
  void load()
}

function reset(): void {
  filters.status = ''
  filters.only_mine = false
  filters.unassigned = false
  filters.waiting_human = false
  filters.keyword = ''
  search()
}

function onSizeChange(size: number): void {
  pageSize.value = size
  page.value = 1
  void load()
}

function goDetail(ticketId: number): void {
  void router.push({ name: 'cs-ticket-detail', params: { id: ticketId } })
}

/**
 * 领取工单 = 把 assignee_id 设成自己。
 *
 * ⚠️ 领取 = 显式传自己的 user_id。
 * api/customerService.ts 的 assignTicket 现在会区分"不传"与"传 null"：
 *   assignTicket(id)         → 请求体不带 assignee_id → 后端填当前用户 → 自己领取
 *   assignTicket(id, null)   → 请求体 assignee_id=null → 后端解释成"取消领取"
 * 这里显式传自己的 id，语义最明确。
 */
async function takeTicket(row: TicketSummary): Promise<void> {
  assigningId.value = row.ticket_id
  try {
    await assignTicket(row.ticket_id, auth.user?.user_id)
    ElMessage.success(`已领取工单 ${row.ticket_no}`)
    await load()
  } catch {
    // 例如已被别人抢先领取（后端返回 409），拦截器已经提示过了
  } finally {
    assigningId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">工单处理</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-select v-model="filters.status" placeholder="全部状态" clearable style="width: 160px">
          <el-option
            v-for="option in TICKET_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-input
          v-model="filters.keyword"
          placeholder="工单号 / 标题 / 内容关键词"
          clearable
          style="width: 240px"
          @keyup.enter="search"
        />

        <el-checkbox v-model="filters.only_mine">只看我领取的</el-checkbox>
        <el-checkbox v-model="filters.unassigned">只看未领取</el-checkbox>
        <el-checkbox v-model="filters.waiting_human">只看待人工确认</el-checkbox>

        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="rows" size="default">
        <el-table-column prop="ticket_no" label="工单号" width="180" />
        <el-table-column label="标题" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">
            {{ truncate(row.title || row.content, 36) }}
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <StatusTag :value="row.type" :map="TICKET_TYPE_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="TICKET_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="提交人" width="90">
          <template #default="{ row }">
            <span class="text-secondary">#{{ row.user_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="领取人" width="100">
          <template #default="{ row }">
            <span v-if="row.assignee_id">#{{ row.assignee_id }}</span>
            <el-tag v-else type="warning" size="small" disable-transitions>未领取</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="goDetail(row.ticket_id)">查看</el-button>
            <!-- 已被领取的单不显示"领取"：避免客服误点把同事的单抢过来（真要转派由后端传 assignee_id） -->
            <el-button
              v-if="!row.assignee_id"
              link
              type="primary"
              :loading="assigningId === row.ticket_id"
              @click="takeTicket(row)"
            >
              领取
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的工单" />
        </template>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="load"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>
