<script setup lang="ts">
/**
 * 商品卡片：商品列表页的最小展示单元（只负责展示 + 抛事件）。
 *
 * 为什么"加入购物车"不在卡片里自己调接口？
 *   加购成功后要同步顶部导航的购物车角标（auth.setCartCount），
 *   角标属于页面/布局的状态；卡片保持"纯展示 + 抛事件"，
 *   这样客服端/管理端也能复用同一张卡片，不会带出用户态的副作用。
 */
import { computed } from 'vue'

import type { ProductInfo } from '@/types'
import { PRODUCT_STATUS_OPTIONS } from '@/utils/dict'
import { formatAmount, truncate } from '@/utils/format'

const props = defineProps<{
  product: ProductInfo
  /** 该卡片正在加购中（由父组件传入，避免同一页多个按钮各自转圈） */
  adding?: boolean
}>()

const emit = defineEmits<{
  (e: 'view', product: ProductInfo): void
  (e: 'add', product: ProductInfo): void
}>()

const offSale = computed(() => props.product.status !== 'ON_SALE')
const soldOut = computed(() => props.product.stock <= 0)
/** 状态文案统一取字典，不要在页面里自己写中文映射 */
const statusLabel = computed(
  () => PRODUCT_STATUS_OPTIONS.find((option) => option.value === props.product.status)?.label ?? '—',
)
</script>

<template>
  <div class="product-card" role="button" tabindex="0" @click="emit('view', product)">
    <h3 class="product-card__name text-ellipsis" :title="product.name">{{ product.name }}</h3>

    <p class="product-card__desc">{{ truncate(product.description || '暂无商品描述', 60) }}</p>

    <div class="product-card__tags">
      <el-tag :type="offSale ? 'info' : 'success'" size="small" disable-transitions>
        {{ statusLabel }}
      </el-tag>
      <el-tag v-if="!offSale && soldOut" type="danger" size="small" disable-transitions>缺货</el-tag>
    </div>

    <div class="product-card__footer">
      <span class="text-price">{{ formatAmount(product.price) }}</span>
      <span class="text-secondary product-card__stock">库存 {{ product.stock }}</span>
    </div>

    <el-button
      class="product-card__add"
      type="primary"
      :loading="adding"
      :disabled="offSale || soldOut"
      @click.stop="emit('add', product)"
    >
      加入购物车
    </el-button>
  </div>
</template>

<style scoped>
/* 只写 style.css 里没有的局部样式，公共类（.product-card__name/__desc/__footer）直接复用 */
.product-card__tags {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}

.product-card__stock {
  font-size: 12px;
}

.product-card__add {
  width: 100%;
  margin-top: 10px;
}
</style>
