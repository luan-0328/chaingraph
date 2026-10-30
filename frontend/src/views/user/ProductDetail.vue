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
  () =>
    PRODUCT_STATUS_OPTIONS.find((option) => option.value === product.value?.status)?.label ?? '—',
)

/** 与商品卡片使用同一套规则：颜色/图标由商品 id 稳定推导 */
const MEDIA_ICONS = ['Goods', 'Box', 'Present', 'Monitor', 'Handbag']
const tone = computed(() => Math.abs(product.value?.product_id ?? 0) % 5)
const icon = computed(
  () => MEDIA_ICONS[Math.abs(product.value?.product_id ?? 0) % MEDIA_ICONS.length] ?? 'Goods',
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
    <div class="detail__crumb">
      <el-button link @click="goList">
        <el-icon><ArrowLeft /></el-icon>
        <span>返回商品列表</span>
      </el-button>
      <span class="detail__crumb-sep">/</span>
      <span class="text-secondary">商品详情</span>
    </div>

    <div v-if="failed" class="page-card">
      <el-result icon="warning" :title="failTitle" :sub-title="failDesc">
        <template #extra>
          <el-button type="primary" @click="goList">返回商品列表</el-button>
        </template>
      </el-result>
    </div>

    <template v-else-if="product">
      <div class="detail">
        <div class="detail__media" :class="`detail__media--${tone}`">
          <el-icon><component :is="icon" /></el-icon>
          <span v-if="offSale" class="detail__media-badge">已下架</span>
          <span v-else-if="soldOut" class="detail__media-badge">已售罄</span>
        </div>

        <div class="detail__info">
          <div class="detail__tags">
            <el-tag :type="offSale ? 'info' : 'success'" disable-transitions>{{
              statusLabel
            }}</el-tag>
            <el-tag v-if="!offSale && soldOut" type="danger" disable-transitions>缺货</el-tag>
            <span class="detail__no">商品编号 #{{ product.product_id }}</span>
          </div>

          <h1 class="detail__name">{{ product.name }}</h1>
          <p class="detail__desc">{{ product.description || '暂无商品描述' }}</p>

          <div class="detail__price-box">
            <div>
              <span class="detail__price-label">商品价格</span>
              <div class="detail__price">{{ formatAmount(product.price) }}</div>
            </div>
            <div class="detail__stock">
              <span class="text-secondary">库存</span>
              <strong>{{ product.stock }}</strong>
              <span class="text-secondary">件</span>
            </div>
          </div>

          <div class="detail__buy">
            <span class="detail__label">购买数量</span>
            <el-input-number
              v-model="quantity"
              :min="1"
              :max="Math.max(1, product.stock)"
              :disabled="offSale || soldOut"
            />
            <span class="text-secondary detail__buy-hint">
              {{
                offSale || soldOut ? '当前不可购买' : `最多可购买 ${Math.max(1, product.stock)} 件`
              }}
            </span>
          </div>

          <div class="detail__actions">
            <el-button
              type="primary"
              size="large"
              :loading="adding"
              :disabled="offSale || soldOut"
              @click="handleAdd"
            >
              <el-icon v-if="!adding"><ShoppingCart /></el-icon>
              <span>加入购物车</span>
            </el-button>
            <el-button size="large" @click="goCart">去购物车</el-button>
          </div>

          <ul class="detail__promises">
            <li>
              <el-icon><Cpu /></el-icon> Agent 自动处理售后
            </li>
            <li>
              <el-icon><CircleCheck /></el-icon> 退款由人工审批兜底
            </li>
            <li>
              <el-icon><DataLine /></el-icon> 全流程进度可追溯
            </li>
          </ul>
        </div>
      </div>

      <div class="page-card detail__section">
        <h3 class="page-card__title">商品信息</h3>
        <p class="pre-wrap detail__desc-full">{{ product.description || '暂无商品描述' }}</p>
        <el-descriptions :column="2" border size="small" class="detail__meta">
          <el-descriptions-item label="商品编号">{{ product.product_id }}</el-descriptions-item>
          <el-descriptions-item label="上架时间">
            {{ formatDateTime(product.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="库存">{{ product.stock }} 件</el-descriptions-item>
          <el-descriptions-item label="状态">{{ statusLabel }}</el-descriptions-item>
        </el-descriptions>
      </div>
    </template>
  </div>
</template>

<style scoped>
.detail__crumb {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 14px;
  font-size: 13px;
}

.detail__crumb-sep {
  color: #c0c6d2;
}

.detail {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
  margin-bottom: 20px;
}

.detail__media {
  position: relative;
  display: grid;
  place-items: center;
  aspect-ratio: 4 / 3;
  border-radius: var(--radius-xl);
  border: 1px solid var(--border-color);
  overflow: hidden;
}

.detail__media .el-icon {
  font-size: 96px;
  opacity: 0.65;
}

.detail__media--0 {
  background: #eef3ff;
  color: #3b6cf0;
}

.detail__media--1 {
  background: #e8f7f1;
  color: #16a374;
}

.detail__media--2 {
  background: #fef6e9;
  color: #d18b12;
}

.detail__media--3 {
  background: #f1edff;
  color: #7357d9;
}

.detail__media--4 {
  background: #fef0f0;
  color: #d9534f;
}

.detail__media-badge {
  position: absolute;
  top: 14px;
  left: 14px;
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  background: rgb(28 35 51 / 62%);
  color: #fff;
  font-size: 12px;
  font-weight: 500;
}

.detail__info {
  padding: 6px 0;
}

.detail__tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.detail__no {
  color: var(--text-secondary);
  font-size: 12px;
  font-family: var(--font-mono);
}

.detail__name {
  margin: 0 0 10px;
  font-size: 26px;
  font-weight: 700;
  line-height: 1.35;
  letter-spacing: -0.5px;
}

.detail__desc {
  margin: 0 0 20px;
  color: var(--text-regular);
  font-size: 13.5px;
  line-height: 1.8;
}

.detail__price-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
  background: #f8f9fd;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  margin-bottom: 18px;
}

.detail__price-label {
  display: block;
  color: var(--text-secondary);
  font-size: 12px;
  margin-bottom: 2px;
}

.detail__price {
  color: var(--price);
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -1px;
  line-height: 1.2;
}

.detail__stock strong {
  font-size: 18px;
  font-weight: 650;
  margin: 0 3px;
}

.detail__buy {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}

.detail__label {
  font-weight: 600;
  font-size: 13.5px;
}

.detail__buy-hint {
  font-size: 12px;
}

.detail__actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 18px;
}

.detail__actions .el-button {
  min-width: 140px;
}

.detail__actions .el-button + .el-button {
  margin-left: 0;
}

.detail__promises {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin: 0;
  padding: 14px 0 0;
  border-top: 1px dashed var(--border-color);
  list-style: none;
  color: var(--text-secondary);
  font-size: 12px;
}

.detail__promises li {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.detail__promises .el-icon {
  color: var(--brand);
}

.detail__section {
  margin-top: 4px;
}

.detail__desc-full {
  margin: 0 0 18px;
  color: var(--text-regular);
  line-height: 1.9;
}

.detail__meta {
  margin: 0;
}

@media (max-width: 900px) {
  .detail {
    grid-template-columns: 1fr;
    gap: 18px;
  }

  .detail__name {
    font-size: 22px;
  }
}
</style>
