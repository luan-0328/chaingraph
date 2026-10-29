<script setup lang="ts">
/**
 * 用户端布局：顶部导航（商城 / 购物车 / 订单 / 售后 / 工单）+ 内容区。
 * v2.md §71：用户端可以稍微偏现代电商，但仍然是浅色企业风格。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import UserMenu from '@/components/common/UserMenu.vue'
import { useAuthStore } from '@/stores/auth'
import { getCart } from '@/api/cart'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const cartCount = computed(() => auth.cartCount)

const activeMenu = computed(() => {
  const path = route.path
  if (path.startsWith('/products')) return '/products'
  if (path.startsWith('/cart')) return '/cart'
  if (path.startsWith('/orders')) return '/orders'
  if (path.startsWith('/after-sales')) return '/after-sales'
  if (path.startsWith('/tickets')) return '/tickets'
  return path
})

onMounted(async () => {
  void auth.fetchCurrentUser()
  // 顶部购物车角标
  if (auth.isLoggedIn) {
    try {
      const cart = await getCart()
      auth.setCartCount(cart.total_quantity)
    } catch {
      /* 忽略 */
    }
  }
})

function go(path: string): void {
  void router.push(path)
}
</script>

<template>
  <div class="layout">
    <header class="layout__header">
      <div class="layout__brand" @click="go('/products')">
        <el-icon><Shop /></el-icon>
        <span>智能电商客服平台</span>
      </div>

      <el-menu :default-active="activeMenu" mode="horizontal" class="layout__nav" :ellipsis="false">
        <el-menu-item index="/products" @click="go('/products')">商品</el-menu-item>
        <el-menu-item index="/orders" @click="go('/orders')">我的订单</el-menu-item>
        <el-menu-item index="/after-sales" @click="go('/after-sales')">我的售后</el-menu-item>
        <el-menu-item index="/tickets" @click="go('/tickets')">我的工单</el-menu-item>
      </el-menu>

      <div class="layout__right">
        <el-badge :value="cartCount" :hidden="cartCount === 0" class="layout__cart">
          <el-button text @click="go('/cart')">
            <el-icon><ShoppingCart /></el-icon>
            <span>购物车</span>
          </el-button>
        </el-badge>
        <UserMenu />
      </div>
    </header>

    <main class="layout__main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.layout__header {
  display: flex;
  align-items: center;
  gap: 24px;
  height: 60px;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid var(--border-color);
  position: sticky;
  top: 0;
  z-index: 10;
}

.layout__brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.layout__nav {
  flex: 1;
  border-bottom: none;
}

.layout__right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.layout__cart {
  margin-right: 4px;
}

.layout__main {
  flex: 1;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px 24px 40px;
}
</style>
