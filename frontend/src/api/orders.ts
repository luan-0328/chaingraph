/** 订单接口（v2.md §45 Order） */

import request from '@/utils/request'
import type {
  LogisticsInfo,
  OrderDetail,
  OrderSummary,
  PageResult,
} from '@/types'

export function createOrder(itemIds?: number[]): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>('/orders', { item_ids: itemIds ?? null })
}

export function listMyOrders(params: {
  limit?: number
  offset?: number
  payment_status?: string
  fulfillment_status?: string
}): Promise<PageResult<OrderSummary>> {
  return request.get<unknown, PageResult<OrderSummary>>('/orders', { params })
}

export function getOrder(orderId: number): Promise<OrderDetail> {
  return request.get<unknown, OrderDetail>(`/orders/${orderId}`)
}

/** 模拟支付（后端幂等：重复支付返回 409） */
export function payOrder(orderId: number): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/orders/${orderId}/pay`)
}

export function cancelOrder(orderId: number): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/orders/${orderId}/cancel`)
}

export function confirmOrder(orderId: number): Promise<OrderDetail> {
  return request.post<unknown, OrderDetail>(`/orders/${orderId}/confirm`)
}

export function getLogistics(orderId: number): Promise<LogisticsInfo> {
  return request.get<unknown, LogisticsInfo>(`/orders/${orderId}/logistics`)
}
