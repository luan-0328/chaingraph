/**
 * Agent 处理进度（SSE）的类型定义。
 *
 * ⚠️ 对应后端 app/agent/progress.py 推出来的事件结构。
 *
 * 注意：这里展示的是**业务进度**（正在分析 / 正在查询订单 / 等待人工确认），
 *      不是 LLM 的思维链 —— 前端文档第二十七节明确禁止展示 Chain of Thought。
 */

import type { TicketStatus } from './ticket'

export type AgentStage =
  | 'analyzing' // 正在分析工单
  | 'executing' // 正在查询/执行（调用工具）
  | 'generating' // 正在生成结论
  | 'waiting_approval' // 等待人工确认
  | 'completed' // 处理完成
  | 'failed' // 处理失败

export type AgentEventType = 'progress' | 'done' | 'error'

export interface AgentProgressEvent {
  type: AgentEventType
  stage: AgentStage
  /** 产生事件的后端节点名（analyze_ticket / tools / finalize），前端只用来调试 */
  node?: string
  /** 如果这条进度来自工具调用，这里是工具名 */
  tool?: string | null
  /** 给用户看的中文文案 */
  message: string
  /** ISO 8601 UTC */
  at?: string

  // ---- 仅 type = 'done' 时有 ----
  ticket_id?: number
  status?: TicketStatus
  analysis?: string
  tools_used?: string[]

  // ---- 仅 type = 'error' 时有 ----
  detail?: string
}
