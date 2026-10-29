/** 购物车接口（v2.md §45 Cart） */

import request from '@/utils/request'
import type { CartItemInfo, CartResponse } from '@/types'

export function getCart(): Promise<CartResponse> {
  return request.get<unknown, CartResponse>('/cart')
}

export function addToCart(data: { product_id: number; quantity: number }): Promise<CartItemInfo> {
  return request.post<unknown, CartItemInfo>('/cart/items', data)
}

export function updateCartItem(itemId: number, quantity: number): Promise<CartItemInfo> {
  return request.patch<unknown, CartItemInfo>(`/cart/items/${itemId}`, { quantity })
}

export function removeCartItem(itemId: number): Promise<void> {
  return request.delete<unknown, void>(`/cart/items/${itemId}`)
}

export function clearCart(): Promise<void> {
  return request.delete<unknown, void>('/cart')
}
