<script setup lang="ts">
/**
 * 工单列表（前端文档第十五、十六节）。
 *
 * 支持：关键词搜索 / 状态筛选 / 类型筛选 / 只看我的 / 分页 / 查看详情 / 新建工单。
 * 数据全部来自后端真实接口 GET /api/tickets（带 total 的分页信封）。
 */
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { listTickets } from '@/api/ticket'
import TicketStatusTag from '@/components/TicketStatusTag.vue'
import type { TicketStatus, TicketSummary, TicketType } from '@/types/ticket'
import { TICKET_STATUS_OPTIONS, TICKET_TYPE_OPTIONS, ticketTypeLabel } from '@/utils/dict'
import { formatDateTime, truncate } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const tickets = ref<TicketSummary[]>([])
const total = ref(0)

const query = reactive({
  keyword: '',
  status: '' as TicketStatus | '',
  type: '' as TicketType | '',
  mine: false,
  page: 1,
  pageSize: 10,
})

async function fetchList(): Promise<void> {
  loading.value = true
  try {
    const result = await listTickets({
      limit: query.pageSize,
      offset: (query.page - 1) * query.pageSize,
      status: query.status,
      type: query.type,
      keyword: query.keyword.trim(),
      mine: query.mine,
    })
    tickets.value = result.items
    total.value = result.total
  } catch {
    // 错误提示已由 axios 拦截器统一处理，这里只需要把列表清空
    tickets.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  query.page = 1
  void fetchList()
}

function handleReset(): void {
  query.keyword = ''
  query.status = ''
  query.type = ''
  query.mine = false
  query.page = 1
  void fetchList()
}

function handlePageChange(page: number): void {
  query.page = page
  void fetchList()
}

function handleSizeChange(size: number): void {
  query.pageSize = size
  query.page = 1
  void fetchList()
}

function goDetail(ticketId: number): void {
  void router.push({ name: 'ticket-detail', params: { id: ticketId } })
}

function goCreate(): void {
  void router.push({ name: 'ticket-create' })
}

onMounted(fetchList)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">工单管理</h2>
      <el-button type="primary" @click="goCreate">
        <el-icon><Plus /></el-icon>
        <span>新建工单</span>
      </el-button>
    </div>

    <div class="page-card">
      <!-- 工具栏：搜索 / 筛选 -->
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="搜索工单编号 / 标题 / 内容"
          clearable
          style="width: 260px"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <el-select
          v-model="query.status"
          placeholder="全部状态"
          clearable
          style="width: 150px"
          @change="handleSearch"
        >
          <el-option
            v-for="option in TICKET_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-select
          v-model="query.type"
          placeholder="全部类型"
          clearable
          style="width: 150px"
          @change="handleSearch"
        >
          <el-option
            v-for="option in TICKET_TYPE_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>

        <el-checkbox v-model="query.mine" @change="handleSearch">只看我的</el-checkbox>

        <el-button @click="handleSearch">查询</el-button>
        <el-button text @click="handleReset">重置</el-button>
      </div>

      <!-- 表格 -->
      <el-table v-loading="loading" :data="tickets" border stripe>
        <el-table-column label="工单编号" width="110">
          <template #default="{ row }">
            <el-button link type="primary" @click="goDetail(row.ticket_id)">
              #{{ row.ticket_id }}
            </el-button>
          </template>
        </el-table-column>

        <el-table-column label="标题" min-width="260">
          <template #default="{ row }">
            <span class="text-ellipsis" :title="row.content">
              {{ row.title || truncate(row.content, 40) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            {{ ticketTypeLabel(row.type) }}
          </template>
        </el-table-column>

        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <TicketStatusTag :status="row.status" />
          </template>
        </el-table-column>

        <el-table-column label="提交人" width="100">
          <template #default="{ row }">用户 #{{ row.user_id }}</template>
        </el-table-column>

        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">
            {{ formatDateTime(row.updated_at) }}
          </template>
        </el-table-column>

        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="goDetail(row.ticket_id)">查看</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无工单">
            <el-button type="primary" @click="goCreate">创建工单</el-button>
          </el-empty>
        </template>
      </el-table>

      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          :current-page="query.page"
          :page-size="query.pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
