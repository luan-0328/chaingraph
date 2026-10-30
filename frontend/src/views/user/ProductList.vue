<script setup lang="ts">
/**
 * 商品列表（用户端首页）：搜索 + 网格卡片 + 分页。
 *
 * 商品数据由页面自己请求（服务端数据不放 Pinia），
 * 但加购成功后要同步顶部导航的购物车角标 —— 那是布局要用的状态，所以放 auth store。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import ProductCard from '@/components/product/ProductCard.vue'
import { addToCart, getCart } from '@/api/cart'
import { listProducts } from '@/api/products'
import { useAuthStore } from '@/stores/auth'
import type { ProductInfo } from '@/types'

const router = useRouter()
const auth = useAuthStore()

/** 一行 4 个卡片，每页 12 条刚好 3 行 */
const PAGE_SIZE = 12

const loading = ref(false)
const keyword = ref('')
const page = ref(1)
const total = ref(0)
const products = ref<ProductInfo[]>([])

/** 只有被点的那张卡片转圈，用 product_id 记录，不要用"全局 loading" */
const addingId = ref<number | null>(null)

const hasFilter = computed(() => keyword.value.trim().length > 0)

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listProducts({
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
      keyword: keyword.value.trim() || undefined,
    })
    products.value = result.items
    total.value = result.total
  } catch {
    // 提示已由 axios 拦截器统一弹出，这里只需保证页面不崩、不留旧数据
    products.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  page.value = 1
  void load()
}

function handleReset(): void {
  keyword.value = ''
  handleSearch()
}

function handlePageChange(next: number): void {
  page.value = next
  void load()
}

function goDetail(product: ProductInfo): void {
  void router.push({ name: 'product-detail', params: { id: product.product_id } })
}

async function handleAdd(product: ProductInfo): Promise<void> {
  addingId.value = product.product_id
  try {
    await addToCart({ product_id: product.product_id, quantity: 1 })
    // 角标要的是"购物车总件数"，而 addToCart 只返回这一行，
    // 所以顺手查一次购物车拿权威数量（同商品会累加，本地 +1 容易算错）
    const cart = await getCart()
    auth.setCartCount(cart.total_quantity)
    ElMessage.success(`已加入购物车：${product.name}`)
  } catch {
    // 库存不足 / 商品已下架等错误，拦截器已经提示过了，不要重复弹
  } finally {
    addingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div>
    <!-- 首屏：一句产品定位 + 搜索，不做轮播图，也不放营销噪音 -->
    <section class="shop-hero">
      <div class="shop-hero__content">
        <p class="shop-hero__eyebrow">智能售后 · 全程可追踪</p>
        <h1 class="shop-hero__title">好物下单，售后无忧</h1>
        <p class="shop-hero__lead">
          订单、售后与工单在同一条链路上。遇到问题提交工单，Agent 会查询真实业务数据并按规则处理。
        </p>

        <div class="shop-hero__search">
          <el-input
            v-model="keyword"
            placeholder="搜索商品名称 / 描述"
            size="large"
            clearable
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-button type="primary" size="large" @click="handleSearch">搜索</el-button>
          <el-button v-if="hasFilter" text size="large" @click="handleReset">清空</el-button>
        </div>
      </div>

      <div class="shop-hero__art" aria-hidden="true">
        <div class="hero-card hero-card--1">
          <span class="hero-card__icon hero-card__icon--green"
            ><el-icon><CircleCheck /></el-icon
          ></span>
          <div>
            <strong>订单已支付</strong>
            <span>等待商家发货</span>
          </div>
        </div>
        <div class="hero-card hero-card--2">
          <span class="hero-card__icon hero-card__icon--blue"
            ><el-icon><Cpu /></el-icon
          ></span>
          <div>
            <strong>Agent 处理中</strong>
            <span>正在核对售后规则</span>
          </div>
        </div>
        <div class="hero-card hero-card--3">
          <span class="hero-card__icon hero-card__icon--amber"
            ><el-icon><ChatDotRound /></el-icon
          ></span>
          <div>
            <strong>工单已提交</strong>
            <span>进度实时推送到这里</span>
          </div>
        </div>
      </div>
    </section>

    <div class="section-head">
      <div class="section-head__left">
        <h2 class="section-head__title">
          {{ hasFilter ? '搜索结果' : '全部商品' }}
        </h2>
        <span class="section-head__meta">
          {{ loading ? '加载中…' : `共 ${total} 件商品` }}
        </span>
      </div>
      <el-tag v-if="hasFilter" closable type="info" @close="handleReset">
        关键词：{{ keyword }}
      </el-tag>
    </div>

    <div v-loading="loading" class="product-grid">
      <el-empty
        v-if="!loading && products.length === 0"
        class="product-grid__empty page-card"
        description="没有找到相关商品"
        :image-size="100"
      >
        <el-button v-if="hasFilter" @click="handleReset">清空搜索条件</el-button>
      </el-empty>

      <ProductCard
        v-for="product in products"
        :key="product.product_id"
        :product="product"
        :adding="addingId === product.product_id"
        @view="goDetail"
        @add="handleAdd"
      />
    </div>

    <div v-if="total > 0" class="pagination">
      <el-pagination
        layout="total, prev, pager, next"
        :total="total"
        :page-size="PAGE_SIZE"
        :current-page="page"
        background
        @current-change="handlePageChange"
      />
    </div>
  </div>
</template>

<style scoped>
.shop-hero {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 32px;
  min-height: 240px;
  padding: 36px 40px;
  margin-bottom: 28px;
  border: 1px solid #dfe7fb;
  border-radius: var(--radius-xl);
  background: #eef3ff;
  overflow: hidden;
}

/* 背景圆环是描边而非渐变，保持 v2.md §71 的浅色、无渐变约束 */
.shop-hero::after {
  content: '';
  position: absolute;
  right: -80px;
  top: -120px;
  width: 360px;
  height: 360px;
  border: 1px solid rgb(59 108 240 / 12%);
  border-radius: 50%;
  pointer-events: none;
}

.shop-hero__content {
  position: relative;
  z-index: 1;
  max-width: 560px;
}

.shop-hero__eyebrow {
  margin: 0 0 10px;
  color: var(--brand);
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 2px;
}

.shop-hero__title {
  margin: 0;
  color: #14204a;
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.8px;
  line-height: 1.25;
}

.shop-hero__lead {
  margin: 12px 0 22px;
  color: #55617a;
  font-size: 13.5px;
  line-height: 1.75;
}

.shop-hero__search {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 460px;
}

.shop-hero__search :deep(.el-input__wrapper) {
  background: #fff;
  box-shadow: 0 4px 12px -4px rgb(20 30 55 / 10%);
}

.shop-hero__art {
  position: relative;
  z-index: 1;
  flex: none;
  width: 300px;
  height: 180px;
}

.hero-card {
  position: absolute;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 210px;
  padding: 12px 14px;
  background: #fff;
  border: 1px solid #e2e9fa;
  border-radius: var(--radius-md);
  box-shadow: 0 12px 24px -14px rgb(20 30 55 / 24%);
}

.hero-card strong {
  display: block;
  font-size: 12.5px;
  font-weight: 600;
  color: #1c2333;
}

.hero-card span {
  display: block;
  margin-top: 1px;
  font-size: 11px;
  color: var(--text-secondary);
}

.hero-card__icon {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  font-size: 15px;
  flex: none;
}

.hero-card__icon--green {
  background: #e8f7f1;
  color: var(--success);
}

.hero-card__icon--blue {
  background: #eef3ff;
  color: var(--brand);
}

.hero-card__icon--amber {
  background: #fef6e9;
  color: #d18b12;
}

.hero-card--1 {
  top: 0;
  left: 40px;
}

.hero-card--2 {
  top: 58px;
  left: 0;
}

.hero-card--3 {
  top: 116px;
  left: 62px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.section-head__left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.section-head__title {
  margin: 0;
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.2px;
}

.section-head__meta {
  color: var(--text-secondary);
  font-size: 12.5px;
}

.product-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  min-height: 220px;
}

.product-grid__empty {
  grid-column: 1 / -1;
  padding: 40px 20px;
}

@media (max-width: 1120px) {
  .shop-hero__art {
    display: none;
  }

  .product-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 860px) {
  .product-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .shop-hero {
    padding: 28px 24px;
  }

  .shop-hero__title {
    font-size: 26px;
  }
}

@media (max-width: 560px) {
  .product-grid {
    grid-template-columns: 1fr;
  }

  .shop-hero__search {
    flex-wrap: wrap;
  }
}
</style>
