"""models：全部 ORM 模型。

表清单（v2.md §53 的数据结构）：
    users                用户（三角色）
    products             商品
    cart_items           购物车
    orders               订单（支付/履约/售后 三状态分离）
    order_items          订单明细（含商品快照）
    payments             支付记录
    logistics            物流
    after_sales          售后单
    refund_records       退款流水
    tickets              工单（含 agent_status：Agent 执行权，用于同工单互斥）
    ticket_messages      工单消息
    agent_runs           Agent 执行记录
    agent_run_events     Agent 业务事件
    knowledge_documents  知识文档
    knowledge_chunks     知识分块 + 向量
    user_memories        用户长期记忆
    side_effect_claims   副作用幂等记录（退款/支付只做一次，靠唯一约束保证）

这个模块必须被 database.init_database() 导入一次，
否则 Base.metadata 里没有这些表，create_all 会"什么都不建"。
"""

from app.models.after_sale import AfterSale, AfterSaleStatus, AfterSaleType
from app.models.agent_event import AgentEventType, AgentRunEvent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.cart import CartItem
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeStatus
from app.models.logistics import Logistics, LogisticsStatus
from app.models.order import (
    FulfillmentStatus,
    Order,
    OrderAfterSaleStatus,
    PaymentStatus,
)
from app.models.order_item import OrderItem
from app.models.payment import Payment, PaymentRecordStatus
from app.models.product import Product, ProductStatus
from app.models.refund import RefundRecord, RefundStatus
from app.models.side_effect import SideEffectClaim, SideEffectKind
from app.models.ticket import AgentStatus, Ticket, TicketPriority, TicketStatus, TicketType
from app.models.ticket_message import SenderType, TicketMessage
from app.models.user import Role, User, UserStatus
from app.models.user_memory import MemoryType, UserMemory

__all__ = [
    # 模型
    "AfterSale",
    "AgentRun",
    "AgentRunEvent",
    "CartItem",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "Logistics",
    "Order",
    "OrderItem",
    "Payment",
    "Product",
    "RefundRecord",
    "SideEffectClaim",
    "Ticket",
    "TicketMessage",
    "User",
    "UserMemory",
    # 枚举
    "AfterSaleStatus",
    "AfterSaleType",
    "AgentEventType",
    "AgentRunStatus",
    "AgentStatus",
    "FulfillmentStatus",
    "KnowledgeStatus",
    "LogisticsStatus",
    "MemoryType",
    "OrderAfterSaleStatus",
    "PaymentRecordStatus",
    "PaymentStatus",
    "ProductStatus",
    "RefundStatus",
    "Role",
    "SenderType",
    "SideEffectKind",
    "TicketPriority",
    "TicketStatus",
    "TicketType",
    "UserStatus",
]
