/**
 * 类型定义 —— **与后端 app/schemas/ 逐字段对应**（唯一契约来源）。
 * 规则（v2.md §83）：不虚构字段、不自己发明枚举。
 */

// ======================================================================
// 通用
// ======================================================================
export interface PageQuery {
  limit?: number
  offset?: number
}

export interface PageResult<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface ApiError {
  code: string
  message: string
  detail?: string
}

// ======================================================================
// 鉴权 / 用户（v2.md §2、§6）
// ======================================================================
export type UserRole = 'USER' | 'CUSTOMER_SERVICE' | 'ADMIN'

export interface LoginRequest {
  username: string
  password: string
}

export interface RegisterRequest {
  username: string
  password: string
  nickname?: string
  email?: string
  phone?: string
}

export interface UserInfo {
  user_id: number
  username: string
  nickname: string | null
  email: string | null
  phone: string | null
  role: UserRole
  created_at: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: UserInfo
}

export interface AdminUserInfo {
  user_id: number
  username: string
  nickname: string | null
  role: UserRole
  status: string
  created_at: string
}

// ======================================================================
// 商品（v2.md §7）
// ======================================================================
export type ProductStatus = 'ON_SALE' | 'OFF_SALE'

export interface ProductInfo {
  product_id: number
  name: string
  description: string | null
  price: number
  stock: number
  status: ProductStatus
  created_at: string
  updated_at: string
}

export interface ProductCreate {
  name: string
  price: number
  stock: number
  description?: string | null
  status?: ProductStatus
}

export type ProductUpdate = Partial<ProductCreate>

// ======================================================================
// 购物车（v2.md §8）
// ======================================================================
export interface CartItemInfo {
  item_id: number
  product_id: number
  product_name: string | null
  price: number | null
  stock: number | null
  product_status: ProductStatus | null
  quantity: number
  subtotal: number | null
  added_at: string
}

export interface CartResponse {
  items: CartItemInfo[]
  total_quantity: number
  total_amount: number
}

// ======================================================================
// 订单（v2.md §9-§13）
// ======================================================================
export type PaymentStatus = 'UNPAID' | 'PAID' | 'REFUNDED'
export type FulfillmentStatus =
  | 'PENDING'
  | 'PROCESSING'
  | 'SHIPPED'
  | 'DELIVERED'
  | 'COMPLETED'
  | 'CANCELLED'
export type OrderAfterSaleStatus = 'NONE' | 'APPLYING' | 'REFUNDING' | 'REFUNDED'

export interface OrderItemInfo {
  product_id: number
  product_name: string
  product_price: number
  quantity: number
  subtotal: number
}

export interface PaymentInfo {
  payment_no: string
  amount: number
  status: string
  paid_at: string | null
}

export interface LogisticsInfo {
  tracking_no: string
  company: string | null
  status: 'PENDING' | 'IN_TRANSIT' | 'DELIVERED'
  latest_message: string | null
  last_update_time: string | null
}

export interface OrderSummary {
  order_id: number
  order_no: string
  total_amount: number
  payment_status: PaymentStatus
  fulfillment_status: FulfillmentStatus
  after_sale_status: OrderAfterSaleStatus
  created_at: string
  updated_at: string
}

export interface OrderDetail extends OrderSummary {
  items: OrderItemInfo[]
  payments: PaymentInfo[]
  logistics: LogisticsInfo | null
}

// ======================================================================
// 售后 / 退款（v2.md §15-§17）
// ======================================================================
export type AfterSaleType = 'REFUND' | 'RETURN' | 'EXCHANGE' | 'OTHER'
export type AfterSaleStatus =
  | 'APPLYING'
  | 'PROCESSING'
  | 'APPROVED'
  | 'REJECTED'
  | 'COMPLETED'
  | 'CANCELLED'

export interface RefundInfo {
  refund_no: string
  amount: number
  status: 'PENDING' | 'SUCCESS' | 'FAILED'
  reason: string | null
  approved_by: number | null
  created_at: string
  completed_at: string | null
}

export interface RefundApprovalContext {
  expected_run_id: number | null
  review_round: number | null
  ticket_id: number | null
  run_status: string | null
  review_payload: { solution?: string; reason?: string; order_snapshot?: Record<string, unknown> } | null
  order_snapshot: Record<string, unknown>
}

export interface AfterSaleInfo {
  approval_context?: RefundApprovalContext | null
  after_sale_id: number
  after_sale_no: string
  order_id: number
  type: AfterSaleType
  status: AfterSaleStatus
  amount: number
  reason: string | null
  refunds: RefundInfo[]
  created_at: string
  updated_at: string
  completed_at: string | null
}

/** POST /after-sales/{id}/refund 的响应：区分"自动退成功"与"转人工审核" */
export interface RefundApplyResponse {
  after_sale: AfterSaleInfo
  executed: boolean
  need_human: boolean
  reason: string
  refund: RefundInfo | null
}

// ======================================================================
// 工单 / 消息 / Agent（v2.md §18-§44）
// ======================================================================
export type TicketStatus =
  | 'OPEN'
  | 'PROCESSING'
  | 'WAITING_HUMAN'
  | 'RESOLVED'
  | 'CLOSED'
  | 'FAILED'
export type TicketType = 'ORDER' | 'LOGISTICS' | 'REFUND' | 'AFTER_SALE' | 'OTHER'
export type TicketPriority = 'LOW' | 'NORMAL' | 'HIGH' | 'URGENT'
/** Agent 单次运行的状态（后端 AgentRunStatus）—— 注意与 TicketStatus 是两套枚举 */
export type AgentRunStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'WAITING_HUMAN'
  | 'COMPLETED'
  | 'FAILED'
  | 'REJECTED'

/** Agent 图里的节点名（后端 agent_runs.current_node）—— 用来显示"此刻跑到哪一步" */
export type AgentNodeName =
  | 'analyze_ticket'
  | 'retrieve_knowledge'
  | 'execute_tools'
  | 'generate_result'
  | 'human_review'
  | 'execute_action'

/**
 * 工单上的 Agent 执行权状态（后端 AgentStatus）—— 第三套枚举，只回答
 * "此刻有没有 Agent 占着这张工单"，与业务状态 status 不同：
 *   status=PROCESSING 且 agent_status=IDLE → 客服在处理，Agent 没在跑
 *   agent_status=RUNNING                   → Agent 正在执行（同工单互斥的依据）
 *   agent_status=WAITING_HUMAN             → 停在人工确认点，等客服审批
 */
export type AgentStatus = 'IDLE' | 'RUNNING' | 'WAITING_HUMAN'
export type SenderType = 'USER' | 'AGENT' | 'CUSTOMER_SERVICE' | 'SYSTEM'

export interface TicketMessageInfo {
  message_id: number
  ticket_id: number
  sender_type: SenderType
  sender_id: number | null
  content: string
  created_at: string
}

export interface AgentEventInfo {
  event_type: string
  message: string
  created_at: string
}

export interface AgentDecision {
  issue_type: TicketType
  summary: string
  need_retrieval: boolean
  need_order_query: boolean
  need_logistics_query: boolean
  need_after_sale_query: boolean
  can_auto_process: boolean
  need_human: boolean
}

export interface AgentResult {
  issue_type: TicketType
  summary: string
  analysis: string
  solution: string
  references: Array<{ title?: string; source?: string; category?: string; score?: number }>
  tool_results: Array<{ tool?: string; ok?: boolean }>
  business_action: string
  need_human: boolean
}

export interface AgentRunInfo {
  review_round: number
  review_payload?: { reason?: string; order_snapshot?: Record<string, unknown> } | null
  execution_epoch?: number
  lease_until?: string | null
  agent_run_id: number
  thread_id: string
  status: AgentRunStatus
  used_rag: boolean
  used_tool: boolean
  need_human: boolean
  /** 这一次执行内部重试了几次（只对可恢复异常重试） */
  retry_count?: number
  /** true = 中途失败过但用断点续跑补上了（复用了已完成的检索/查询结果） */
  resumed?: boolean
  /** 此刻正在跑哪个节点（执行结束/失败时为空） */
  current_node?: AgentNodeName | string | null
  started_at: string | null
  finished_at: string | null
  /** 心跳时间：Agent 每个业务事件都会刷新，用于识别僵尸执行 */
  alive_at?: string | null
  error: string | null
  decision: AgentDecision | null
  result: AgentResult | null
}

export interface TicketSummary {
  ticket_id: number
  ticket_no: string
  user_id: number
  title: string | null
  content: string
  type: TicketType | null
  priority: TicketPriority
  status: TicketStatus
  /** Agent 执行权状态（同工单互斥的依据），与 status 含义不同 */
  agent_status?: AgentStatus
  agent_run_id?: number | null
  assignee_id: number | null
  order_id: number | null
  after_sale_id: number | null
  created_at: string
  updated_at: string
}

export interface TicketDetail extends TicketSummary {
  messages: TicketMessageInfo[]
  agent_run: AgentRunInfo | null
  agent_events: AgentEventInfo[]
  agent_runs: AgentRunInfo[]
}

export interface AgentProcessResponse {
  ticket_id: number
  agent_run_id: number
  thread_id: string
  /**
   * 本次真的执行了 → 取值同 AgentRunStatus（不是工单状态）；
   * 同工单已有执行在跑（并发抢占失败）→ 'ALREADY_RUNNING'，此时 agent_run_id 指向当前那次执行。
   */
  status: AgentRunStatus | 'ALREADY_RUNNING'
  need_human: boolean
  decision: AgentDecision | null
  result: AgentResult | null
  /** 工单上的 Agent 执行权状态 */
  agent_status?: AgentStatus | null
  /** true = 复用了已有执行，本次没有新建 AgentRun */
  used_existing_run?: boolean
  message?: string | null
}

/** SSE 推送的事件（后端 app/agent/events.py） */
export interface AgentStreamEvent {
  type: 'event' | 'done' | 'error'
  event_type?: string
  message: string
  agent_run_id?: number | null
  ticket_id?: number
  status?: string
  detail?: string
  at?: string
  /**
   * 实时通道的单调递增序号（同时作为 SSE 的 `id:` 下发）。
   * 断线重连时把它作为 `Last-Event-ID` 传回去，就能只补收缺失的那一段 ——
   * 它不是业务主键，审计以数据库里的 agent_run_events 为准。
   */
  seq?: number
}

// ======================================================================
// 客服工作台 / 管理端
// ======================================================================
export interface DashboardStats {
  ticket_total: number
  ticket_by_status: Record<string, number>
  order_total: number
  order_paid: number
  after_sale_pending: number
  agent_run_by_status: Record<string, number>
  product_on_sale: number
}

export interface KnowledgeInfo {
  document_id: number
  title: string
  content: string
  category: string | null
  source: string | null
  version: string
  status: string
  created_at: string
  updated_at: string
}

export interface KnowledgeSearchHit {
  title: string | null
  source: string | null
  category: string | null
  content: string
  score: number
}
