/** 售后接口（v2.md §45 AfterSale） */

import request from '@/utils/request'
import type {
  AfterSaleInfo,
  AfterSaleType,
  PageResult,
  RefundApplyResponse,
} from '@/types'

export function createAfterSale(data: {
  order_id: number
  type?: AfterSaleType
  reason: string
  amount?: number | null
}): Promise<AfterSaleInfo> {
  return request.post<unknown, AfterSaleInfo>('/after-sales', data)
}

export function listMyAfterSales(params: {
  limit?: number
  offset?: number
}): Promise<PageResult<AfterSaleInfo>> {
  return request.get<unknown, PageResult<AfterSaleInfo>>('/after-sales', { params })
}

export function getAfterSale(afterSaleId: number): Promise<AfterSaleInfo> {
  return request.get<unknown, AfterSaleInfo>(`/after-sales/${afterSaleId}`)
}

export function cancelAfterSale(afterSaleId: number, reason?: string): Promise<AfterSaleInfo> {
  return request.post<unknown, AfterSaleInfo>(`/after-sales/${afterSaleId}/cancel`, { reason })
}

/**
 * 申请退款。
 * 注意返回值：executed=true 表示已自动退款；need_human=true 表示转人工审核（前端要如实提示）。
 */
export function applyRefund(afterSaleId: number): Promise<RefundApplyResponse> {
  return request.post<unknown, RefundApplyResponse>(`/after-sales/${afterSaleId}/refund`)
}
