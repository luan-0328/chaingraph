/** 管理端接口（v2.md §52） */

import request from '@/utils/request'
import type {
  AdminUserInfo,
  KnowledgeInfo,
  KnowledgeSearchHit,
  PageResult,
  ProductCreate,
  ProductInfo,
  ProductUpdate,
} from '@/types'

// ---------------- 商品管理 ----------------
export function listProducts(params: {
  limit?: number
  offset?: number
  keyword?: string
  status?: string
}): Promise<PageResult<ProductInfo>> {
  return request.get<unknown, PageResult<ProductInfo>>('/admin/products', { params })
}

export function createProduct(data: ProductCreate): Promise<ProductInfo> {
  return request.post<unknown, ProductInfo>('/admin/products', data)
}

export function updateProduct(productId: number, data: ProductUpdate): Promise<ProductInfo> {
  return request.patch<unknown, ProductInfo>(`/admin/products/${productId}`, data)
}

/** 下架（软删除：历史订单明细要能追溯商品） */
export function takeOffSale(productId: number): Promise<ProductInfo> {
  return request.delete<unknown, ProductInfo>(`/admin/products/${productId}`)
}

// ---------------- 知识库管理 ----------------
export function listKnowledge(params: {
  limit?: number
  offset?: number
  category?: string
  keyword?: string
}): Promise<PageResult<KnowledgeInfo>> {
  return request.get<unknown, PageResult<KnowledgeInfo>>('/admin/knowledge', { params })
}

export function createKnowledge(data: {
  title: string
  content: string
  category?: string
  source?: string
}): Promise<KnowledgeInfo> {
  return request.post<unknown, KnowledgeInfo>('/admin/knowledge', data)
}

export function updateKnowledge(
  documentId: number,
  data: { title?: string; content?: string; category?: string; status?: string },
): Promise<KnowledgeInfo> {
  return request.patch<unknown, KnowledgeInfo>(`/admin/knowledge/${documentId}`, data)
}

export function deleteKnowledge(documentId: number): Promise<void> {
  return request.delete<unknown, void>(`/admin/knowledge/${documentId}`)
}

/** 知识入库：切分 + 向量化（RAG 的数据准备） */
export function ingestKnowledge(
  force = false,
): Promise<{ documents: number; chunks: number }> {
  return request.post<unknown, { documents: number; chunks: number }>(
    '/admin/knowledge/ingest',
    null,
    { params: { force } },
  )
}

/** 知识检索调试：直接看命中了哪些规则 */
export function searchKnowledge(
  query: string,
  topK = 4,
): Promise<{ query: string; hits: KnowledgeSearchHit[] }> {
  return request.get<unknown, { query: string; hits: KnowledgeSearchHit[] }>(
    '/admin/knowledge/search',
    { params: { query, top_k: topK } },
  )
}

// ---------------- 全量数据 ----------------
export function listAllOrders(params: {
  limit?: number
  offset?: number
}): Promise<PageResult<Record<string, unknown>>> {
  return request.get<unknown, PageResult<Record<string, unknown>>>('/admin/orders', { params })
}

export function listAllTickets(params: {
  limit?: number
  offset?: number
}): Promise<PageResult<Record<string, unknown>>> {
  return request.get<unknown, PageResult<Record<string, unknown>>>('/admin/tickets', { params })
}

export function listAllUsers(params: {
  limit?: number
  offset?: number
  keyword?: string
  role?: string
}): Promise<PageResult<AdminUserInfo>> {
  return request.get<unknown, PageResult<AdminUserInfo>>('/admin/users', { params })
}
