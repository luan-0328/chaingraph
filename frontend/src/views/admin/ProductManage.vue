<script setup lang="ts">
/**
 * 商品管理（路由 admin-products，v2.md §7/§52）。
 *
 * 两个要点：
 * 1. 新建与编辑共用一个对话框：字段完全一样，拆两个对话框只会重复校验规则；
 * 2. "下架"在后端是**软删除**（DELETE /admin/products/{id} → takeOffSale）：
 *    商品不再对用户售卖，但历史订单明细还要能追溯商品名和价格，所以数据不真删。
 *    UI 上因此叫"下架"而不是"删除"，确认框里也把这点讲清楚。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import { createProduct, listProducts, takeOffSale, updateProduct } from '@/api/admin'
import StatusTag from '@/components/common/StatusTag.vue'
import type { ProductCreate, ProductInfo, ProductStatus } from '@/types'
import { PRODUCT_STATUS_OPTIONS } from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/**
 * 字典 PRODUCT_STATUS_OPTIONS 只给了 value/label（本来就没有标签色），
 * 这里补一层颜色交给公共 StatusTag 渲染 —— 颜色属于展示层，不该塞进字典。
 */
const PRODUCT_STATUS_MAP: Record<string, { label: string; tag: TagType }> = Object.fromEntries(
  PRODUCT_STATUS_OPTIONS.map((option) => [
    option.value,
    { label: option.label, tag: option.value === 'ON_SALE' ? 'success' : 'info' },
  ]),
)

/** 库存低于该值时标红，提醒运营补货（阈值放常量，避免模板里出现魔法数字） */
const LOW_STOCK = 10

const loading = ref(false)
const products = ref<ProductInfo[]>([])

const query = reactive<{ keyword: string; status: string | undefined }>({
  keyword: '',
  status: undefined,
})
const pagination = reactive({ current: 1, limit: 10, total: 0 })

async function loadProducts(): Promise<void> {
  loading.value = true
  try {
    const result = await listProducts({
      limit: pagination.limit,
      offset: (pagination.current - 1) * pagination.limit,
      // 空字符串会被后端当成有效筛选值，所以统一转成 undefined（= 不筛选）
      keyword: query.keyword.trim() || undefined,
      status: query.status || undefined,
    })
    products.value = result.items
    pagination.total = result.total
  } finally {
    // 出错的处理（弹提示）由 request 拦截器统一负责，这里只管收起 loading
    loading.value = false
  }
}

function search(): void {
  pagination.current = 1
  void loadProducts()
}

function resetQuery(): void {
  query.keyword = ''
  query.status = undefined
  search()
}

function handlePageChange(page: number): void {
  pagination.current = page
  void loadProducts()
}

function handleSizeChange(size: number): void {
  pagination.limit = size
  pagination.current = 1
  void loadProducts()
}

// ======================================================================
// 新建 / 编辑（同一个对话框）
// ======================================================================
const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
/** null = 新建；有值 = 编辑该商品 */
const editingId = ref<number | null>(null)

const form = reactive<{
  name: string
  price: number | undefined
  stock: number | undefined
  description: string
  status: ProductStatus
}>({
  name: '',
  // 用 undefined 而不是 0：让"必填"校验能真正拦住空值，0 是合法之外的值但不好区分
  price: undefined,
  stock: undefined,
  description: '',
  status: 'ON_SALE',
})

const dialogTitle = computed(() =>
  editingId.value === null ? '新建商品' : `编辑商品 #${editingId.value}`,
)

const rules: FormRules = {
  name: [
    { required: true, message: '请输入商品名称', trigger: 'blur' },
    { max: 128, message: '商品名称最多 128 个字符', trigger: 'blur' },
  ],
  price: [
    { required: true, message: '请输入价格', trigger: 'blur' },
    {
      // 业务要求：价格必须大于 0（后端也会校验，这里只是提前拦下）
      validator: (_rule, value: number | undefined, callback) => {
        if (value === undefined || value === null) callback(new Error('请输入价格'))
        else if (value <= 0) callback(new Error('价格必须大于 0'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
  stock: [
    { required: true, message: '请输入库存', trigger: 'blur' },
    {
      validator: (_rule, value: number | undefined, callback) => {
        if (value === undefined || value === null) callback(new Error('请输入库存'))
        else if (!Number.isInteger(value) || value < 0) {
          callback(new Error('库存必须是不小于 0 的整数'))
        } else callback()
      },
      trigger: 'blur',
    },
  ],
}

function openCreate(): void {
  editingId.value = null
  form.name = ''
  form.price = undefined
  form.stock = undefined
  form.description = ''
  form.status = 'ON_SALE'
  dialogVisible.value = true
  // 对话框关闭后 DOM 仍在，上一次的红字校验会残留，所以每次打开都清一次
  void nextTick(() => formRef.value?.clearValidate())
}

function openEdit(row: ProductInfo): void {
  editingId.value = row.product_id
  form.name = row.name
  form.price = row.price
  form.stock = row.stock
  form.description = row.description ?? ''
  form.status = row.status
  dialogVisible.value = true
  void nextTick(() => formRef.value?.clearValidate())
}

async function submitForm(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const payload: ProductCreate = {
      name: form.name.trim(),
      price: Number(form.price ?? 0),
      stock: Number(form.stock ?? 0),
      // 描述留空时传 null：后端 PATCH 会把 null 当成"清空描述"
      description: form.description.trim() || null,
      status: form.status,
    }

    if (editingId.value === null) {
      await createProduct(payload)
      ElMessage.success('商品已创建')
    } else {
      await updateProduct(editingId.value, payload)
      ElMessage.success('商品已更新')
    }

    dialogVisible.value = false
    await loadProducts()
  } catch {
    // 失败原因（如名称重复、参数不合法）已由拦截器提示，保持对话框打开让用户改
  } finally {
    submitting.value = false
  }
}

// ======================================================================
// 上下架
// ======================================================================
async function handleTakeOffSale(row: ProductInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认下架「${row.name}」？下架是软删除：商品不再对用户售卖，但历史订单明细仍需追溯该商品，数据不会真正删除。`,
      '下架商品',
      { type: 'warning', confirmButtonText: '确认下架', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户点了取消 / 关闭，什么都不做
  }

  try {
    await takeOffSale(row.product_id)
    ElMessage.success('商品已下架')
    await loadProducts()
  } catch {
    // 拦截器已提示
  }
}

async function handleOnSale(row: ProductInfo): Promise<void> {
  try {
    // 上架没有单独接口，走部分更新的 PATCH 语义
    await updateProduct(row.product_id, { status: 'ON_SALE' })
    ElMessage.success('商品已上架')
    await loadProducts()
  } catch {
    // 拦截器已提示
  }
}

onMounted(loadProducts)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">商品管理</h2>
      <el-button type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>
        <span>新建商品</span>
      </el-button>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="按商品名称搜索"
          clearable
          style="width: 240px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select
          v-model="query.status"
          placeholder="全部状态"
          clearable
          style="width: 140px"
          @change="search"
        >
          <el-option
            v-for="option in PRODUCT_STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetQuery">重置</el-button>
      </div>

      <el-table v-loading="loading" :data="products" border stripe>
        <el-table-column prop="product_id" label="ID" width="80" />
        <el-table-column prop="name" label="商品名称" min-width="220" show-overflow-tooltip />
        <el-table-column label="价格" width="120" align="right">
          <template #default="{ row }">
            <span class="text-price">{{ formatAmount(row.price) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="库存" width="110" align="center">
          <template #default="{ row }">
            <!-- 低库存标红：比看纯数字更容易被运营注意到 -->
            <el-tag v-if="row.stock < LOW_STOCK" type="danger" disable-transitions>
              {{ row.stock }}
            </el-tag>
            <span v-else>{{ row.stock }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusTag :value="row.status" :map="PRODUCT_STATUS_MAP" />
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="row.status === 'ON_SALE'"
              link
              type="danger"
              @click="handleTakeOffSale(row)"
            >
              下架
            </el-button>
            <el-button v-else link type="primary" @click="handleOnSale(row)">上架</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="没有符合条件的商品" />
        </template>
      </el-table>

      <div class="pagination">
        <el-pagination
          :current-page="pagination.current"
          :page-size="pagination.limit"
          :page-sizes="[10, 20, 50]"
          :total="pagination.total"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="handlePageChange"
          @size-change="handleSizeChange"
        />
      </div>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogTitle"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
        <el-form-item label="商品名称" prop="name">
          <el-input v-model="form.name" placeholder="例如：无线蓝牙耳机" clearable />
        </el-form-item>
        <el-form-item label="价格" prop="price">
          <el-input-number
            v-model="form.price"
            :min="0"
            :precision="2"
            :step="1"
            controls-position="right"
            style="width: 200px"
          />
        </el-form-item>
        <el-form-item label="库存" prop="stock">
          <el-input-number
            v-model="form.stock"
            :min="0"
            :precision="0"
            :step="1"
            controls-position="right"
            style="width: 200px"
          />
        </el-form-item>
        <el-form-item label="商品描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="卖点、规格等，可留空"
          />
        </el-form-item>
        <el-form-item label="状态" prop="status">
          <el-select v-model="form.status" style="width: 200px">
            <el-option
              v-for="option in PRODUCT_STATUS_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  margin-bottom: 12px;
}
</style>
