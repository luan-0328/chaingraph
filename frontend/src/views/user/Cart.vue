<script setup lang="ts">
/**
 * 购物车：列出购物车行、改数量、删除、选中部分行结算下单。
 *
 * 状态都来自服务端（GET /cart）：
 *   改数量后重新拉一次购物车，让 subtotal / total_amount 由后端算 ——
 *   前端自己算等于把"金额口径"实现两遍，早晚会不一致。
 * 顶部角标同步 auth.setCartCount，改完就刷新。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type TableInstance } from 'element-plus'

import { createOrder } from '@/api/orders'
import { getCart, removeCartItem, updateCartItem } from '@/api/cart'
import { useAuthStore } from '@/stores/auth'
import type { CartItemInfo } from '@/types'
import { formatAmount } from '@/utils/format'

const router = useRouter()
const auth = useAuthStore()

const loading = ref(false)
const submitting = ref(false)
const items = ref<CartItemInfo[]>([])
const selectedRows = ref<CartItemInfo[]>([])
const tableRef = ref<TableInstance | null>(null)

/** 失效行：商品被删除（product_name 为 null）或已下架，不能结算 */
function isInvalid(row: CartItemInfo): boolean {
  return row.product_name === null || row.product_status !== 'ON_SALE'
}

/** 给 el-table 的 selection 列用（失效行不允许勾选） */
function selectable(row: CartItemInfo): boolean {
  return !isInvalid(row)
}

const validRows = computed(() => items.value.filter((row) => !isInvalid(row)))

/** 合计按**选中行**算：下单只结算选中的行，显示全部金额容易让人误判要付多少钱 */
const selectedCount = computed(() => selectedRows.value.reduce((sum, row) => sum + row.quantity, 0))
const selectedAmount = computed(() =>
  selectedRows.value.reduce((sum, row) => sum + (row.subtotal ?? 0), 0),
)

async function syncCartCount(): Promise<void> {
  try {
    const cart = await getCart()
    auth.setCartCount(cart.total_quantity)
  } catch {
    /* 角标同步失败不影响主流程 */
  }
}

/**
 * 默认勾选所有**可结算**的行。
 * 为什么不默认不勾？购物车主按钮"结算下单"在零勾选时是禁用的，
 * 用户点进购物车看到唯一的按钮是灰的，会以为页面坏了；主流电商也都是默认全选。
 * 失效行（下架/已删除）不勾，避免把注定失败的行带进结算。
 */
async function selectAllValid(): Promise<void> {
  await nextTick() // 等表格把新的 items 渲染成行之后才能勾选
  const table = tableRef.value
  if (!table) return
  table.clearSelection()
  validRows.value.forEach((row) => table.toggleRowSelection(row, true))
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const cart = await getCart()
    items.value = cart.items
    auth.setCartCount(cart.total_quantity)
    await selectAllValid()
  } catch {
    items.value = []
    selectedRows.value = []
  } finally {
    loading.value = false
  }
}

function handleSelectionChange(rows: CartItemInfo[]): void {
  // 失效行不给勾选（:selectable 已经限制），这里再兜一层
  selectedRows.value = rows.filter((row) => !isInvalid(row))
}

/** 数量改动即提交：购物车是服务端数据，不该攒到"保存"按钮才同步 */
async function handleQuantityChange(row: CartItemInfo, value?: number): Promise<void> {
  if (!value || value < 1 || value === row.quantity) return
  try {
    await updateCartItem(row.item_id, value)
    await load()
  } catch {
    // 库存不足等错误拦截器已提示，重新加载回滚显示
    await load()
  }
}

async function handleRemove(row: CartItemInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认从购物车删除「${row.product_name ?? '该商品'}」？`, '提示', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  try {
    await removeCartItem(row.item_id)
    ElMessage.success('已删除')
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function handleCheckout(): Promise<void> {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先勾选要结算的商品')
    return
  }
  submitting.value = true
  try {
    const order = await createOrder(selectedRows.value.map((row) => row.item_id))
    ElMessage.success(`下单成功：${order.order_no}`)
    await syncCartCount()
    // 下单后最重要的事是付款，所以直接进订单详情
    await router.push({ name: 'order-detail', params: { id: order.order_id } })
  } catch {
    // 库存不足 / 商品下架等，拦截器已提示；重新拉一次购物车让页面反映真实情况
    await load()
  } finally {
    submitting.value = false
  }
}

function goProducts(): void {
  void router.push({ name: 'product-list' })
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-header">
      <h2 class="page-header__title">购物车</h2>
      <el-button text @click="goProducts">继续购物</el-button>
    </div>

    <div class="page-card">
      <el-empty v-if="!loading && items.length === 0" description="购物车还是空的">
        <el-button type="primary" @click="goProducts">去逛逛</el-button>
      </el-empty>

      <template v-else>
        <el-table
          ref="tableRef"
          v-loading="loading"
          :data="items"
          row-key="item_id"
          @selection-change="handleSelectionChange"
        >
          <el-table-column type="selection" width="48" :selectable="selectable" />

          <el-table-column label="商品" min-width="220">
            <template #default="{ row }">
              <span v-if="isInvalid(row)" class="text-secondary">
                {{ row.product_name ?? '商品已失效' }}（不可结算）
              </span>
              <router-link
                v-else
                :to="{ name: 'product-detail', params: { id: row.product_id } }"
                class="cart__link"
              >
                {{ row.product_name }}
              </router-link>
            </template>
          </el-table-column>

          <el-table-column label="单价" width="120" align="right">
            <template #default="{ row }">{{ formatAmount(row.price) }}</template>
          </el-table-column>

          <el-table-column label="数量" width="160" align="center">
            <template #default="{ row }">
              <el-input-number
                :model-value="row.quantity"
                :min="1"
                :max="Math.max(1, row.stock ?? 999)"
                :disabled="isInvalid(row)"
                size="small"
                @change="handleQuantityChange(row, $event)"
              />
            </template>
          </el-table-column>

          <el-table-column label="小计" width="140" align="right">
            <template #default="{ row }">
              <span class="text-price">{{ formatAmount(row.subtotal) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="100" align="center">
            <template #default="{ row }">
              <el-button type="danger" link @click="handleRemove(row)">删除</el-button>
            </template>
          </el-table-column>

          <template #empty>
            <el-empty description="购物车还是空的" :image-size="60" />
          </template>
        </el-table>

        <div class="cart__footer">
          <div class="cart__summary">
            <span class="text-secondary">
              已选 {{ selectedRows.length }} 种 / {{ selectedCount }} 件（共 {{ validRows.length }} 种可结算）
            </span>
            <span class="cart__amount">
              合计：<span class="text-price">{{ formatAmount(selectedAmount) }}</span>
            </span>
          </div>

          <el-button
            type="primary"
            size="large"
            :loading="submitting"
            :disabled="selectedRows.length === 0"
            @click="handleCheckout"
          >
            结算下单
          </el-button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.cart__link {
  color: var(--text-primary);
}

.cart__link:hover {
  color: var(--brand);
}

.cart__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border-color);
}

.cart__summary {
  display: flex;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
}

.cart__amount {
  font-size: 16px;
}
</style>
