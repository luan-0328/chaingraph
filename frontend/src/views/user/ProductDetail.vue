<script setup lang="ts">
/**
 * 商品详情：商品信息 + 数量选择 + 加入购物车 / 去购物车。
 *
 * 404（商品不存在）和 409（PRODUCT_OFF_SALE 已下架）不是"系统错误"，
 * 而是正常的业务结果 —— 所以用 el-result 页面态呈现，而不是弹一个红色 toast 了事。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { AxiosError } from 'axios'

import { addToCart, getCart } from '@/api/cart'
import { getProduct } from '@/api/products'
import { useAuthStore } from '@/stores/auth'
import type { ApiError, ProductInfo } from '@/types'
import { PRODUCT_STATUS_OPTIONS } from '@/utils/dict'
import { formatAmount, formatDateTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const productId = computed(() => Number(route.params.id))

const loading = ref(true)
const product = ref<ProductInfo | null>(null)
const failed = ref(false)
const failTitle = ref('商品不可购买')
const failDesc = ref('')
const quantity = ref(1)
const adding = ref(false)

const offSale = computed(() => product.value?.status !== 'ON_SALE')
const soldOut = computed(() => (product.value?.stock ?? 0) <= 0)
const statusLabel = computed(
  () => PRODUCT_STATUS_OPTIONS.find((option) => option.value === product.value?.status)?.label ?? '—',
)

async function load(): Promise<void> {
  loading.value = true
  failed.value = false
  try {
    product.value = await getProduct(productId.value)
    // 库存变化后要把数量夹回可选范围，否则会提交一个必然失败的数量
    quantity.value = Math.min(Math.max(1, quantity.value), Math.max(1, product.value.stock))
  } catch (error) {
    product.value = null
    failed.value = true
    const axiosError = error as AxiosError<ApiError>
    const status = axiosError.response?.status
    if (status === 409) {
      failTitle.value = '商品已下架'
      failDesc.value = axiosError.response?.data?.message || '该商品已下架，看看别的吧。'
    } else if (status === 404) {
      failTitle.value = '商品不存在'
      failDesc.value = '商品可能已被删除，返回列表看看其他商品。'
    } else {
      failTitle.value = '加载失败'
      failDesc.value = axiosError.response?.data?.message || '请稍后重试。'
    }
  } finally {
    loading.value = false
  }
}

async function handleAdd(): Promise<void> {
  if (!product.value) return
  adding.value = true
  try {
    await addToCart({ product_id: product.value.product_id, quantity: quantity.value })
    const cart = await getCart()
    auth.setCartCount(cart.total_quantity)
    ElMessage.success('已加入购物车')
  } catch {
    // 拦截器已提示（库存不足 / 已下架）
  } finally {
    adding.value = false
  }
}

function goCart(): void {
  void router.push({ name: 'cart' })
}

function goList(): void {
  void router.push({ name: 'product-list' })
}

onMounted(load)

// 从列表点另一个商品时组件会被复用，靠 watch 参数重新加载
watch(productId, () => {
  quantity.value = 1
  void load()
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-header">
      <h2 class="page-header__title">商品详情</h2>
      <el-button text @click="goList">返回商品列表</el-button>
    </div>

    <div class="page-card">
      <el-result v-if="failed" icon="warning" :title="failTitle" :sub-title="failDesc">
        <template #extra>
          <el-button type="primary" @click="goList">返回商品列表</el-button>
        </template>
      </el-result>

      <template v-else-if="product">
        <h3 class="detail__name">{{ product.name }}</h3>

        <div class="detail__tags">
          <el-tag :type="offSale ? 'info' : 'success'" disable-transitions>{{ statusLabel }}</el-tag>
          <el-tag v-if="!offSale && soldOut" type="danger" disable-transitions>缺货</el-tag>
        </div>

        <div class="detail__price">
          <span class="text-price detail__price-value">{{ formatAmount(product.price) }}</span>
          <span class="text-secondary">库存 {{ product.stock }} 件</span>
        </div>

        <el-descriptions :column="2" border size="small" class="detail__meta">
          <el-descriptions-item label="商品编号">{{ product.product_id }}</el-descriptions-item>
          <el-descriptions-item label="上架时间">
            {{ formatDateTime(product.created_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="detail__block">
          <div class="detail__label">商品描述</div>
          <div class="pre-wrap text-regular">{{ product.description || '暂无商品描述' }}</div>
        </div>

        <div class="detail__buy">
          <span class="detail__label">购买数量</span>
          <el-input-number
            v-model="quantity"
            :min="1"
            :max="Math.max(1, product.stock)"
            :disabled="offSale || soldOut"
          />
          <el-button
            type="primary"
            :loading="adding"
            :disabled="offSale || soldOut"
            @click="handleAdd"
          >
            加入购物车
          </el-button>
          <el-button @click="goCart">去购物车</el-button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.detail__name {
  margin: 0 0 8px;
  font-size: 20px;
}

.detail__tags {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}

.detail__price {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 16px;
}

.detail__price-value {
  font-size: 24px;
}

.detail__meta {
  margin-bottom: 16px;
}

.detail__block {
  margin-bottom: 20px;
}

.detail__label {
  font-weight: 600;
  margin-bottom: 6px;
}

.detail__buy {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-top: 12px;
  border-top: 1px solid var(--border-color);
}
</style>
