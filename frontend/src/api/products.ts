/** 商品接口（用户端只读；管理端在 admin.ts） */

import request from '@/utils/request'
import type { PageResult, ProductInfo } from '@/types'

export function listProducts(params: {
  limit?: number
  offset?: number
  keyword?: string
}): Promise<PageResult<ProductInfo>> {
  return request.get<unknown, PageResult<ProductInfo>>('/products', { params })
}

export function getProduct(productId: number): Promise<ProductInfo> {
  return request.get<unknown, ProductInfo>(`/products/${productId}`)
}
