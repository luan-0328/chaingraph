<script setup lang="ts">
/**
 * 商品列表（用户端首页）：搜索 + 网格卡片 + 分页。
 *
 * 商品数据由页面自己请求（服务端数据不放 Pinia），
 * 但加购成功后要同步顶部导航的购物车角标 —— 那是布局要用的状态，所以放 auth store。
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import ProductCard from '@/components/product/ProductCard.vue'
import { addToCart, getCart } from '@/api/cart'
import { listProducts } from '@/api/products'
import { useAuthStore } from '@/stores/auth'
import type { ProductInfo } from '@/types'

const router = useRouter()
const auth = useAuthStore()

/** 一行 4 个卡片（el-col :span="6"），所以每页 12 条刚好 3 行 */
const PAGE_SIZE = 12

const loading = ref(false)
const keyword = ref('')
const page = ref(1)
const total = ref(0)
const products = ref<ProductInfo[]>([])

/** 只有被点的那张卡片转圈，用 product_id 记录，不要用"全局 loading" */
const addingId = ref<number | null>(null)

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
    <div class="page-header">
      <h2 class="page-header__title">商品</h2>
    </div>

    <div class="page-card">
      <div class="toolbar">
        <el-input
          v-model="keyword"
          placeholder="搜索商品名称 / 描述"
          clearable
          style="width: 280px"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-button type="primary" @click="handleSearch">查询</el-button>
        <el-button @click="handleReset">重置</el-button>
      </div>

      <div v-loading="loading" class="product-grid">
        <el-empty
          v-if="!loading && products.length === 0"
          description="没有找到相关商品"
          :image-size="100"
        >
          <el-button v-if="keyword" @click="handleReset">清空搜索条件</el-button>
        </el-empty>

        <el-row v-else :gutter="16">
          <el-col v-for="product in products" :key="product.product_id" :span="6">
            <div class="product-grid__cell">
              <ProductCard
                :product="product"
                :adding="addingId === product.product_id"
                @view="goDetail"
                @add="handleAdd"
              />
            </div>
          </el-col>
        </el-row>
      </div>

      <!-- 只要有数据就显示分页器（后端返回的 total 才是权威的总数） -->
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
  </div>
</template>

<style scoped>
.product-grid {
  min-height: 200px;
}

/* el-col 之间需要纵向间距，卡片本身 height:100% 撑满找齐 */
.product-grid__cell {
  margin-bottom: 16px;
}
</style>
