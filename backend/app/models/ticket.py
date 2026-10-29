"""工单（v2.md §18.1、§19）。

和 v1 的差别：
    - 有 ticket_no（对用户友好，也便于客服口头沟通）
    - 关联 order_id / after_sale_id（工单要带业务上下文）
    - 有 assignee_id（客服"领取工单"）
    - 有 resolved_at / closed_at（解决与关闭是两个动作：解决后等用户确认才关闭）
    - 有 agent_status / agent_run_id（**Agent 执行权的抢占状态**，见下）

为什么工单上要单独有 agent_status
---------------------------------
status 表达的是"这张工单在业务流程里处于什么位置"（待处理/处理中/已解决…），
agent_status 表达的是"此刻有没有 Agent 正在占用这张工单"。
两者是不同的东西：

    status = PROCESSING 且 agent_status = IDLE    → 客服正在手工处理，Agent 没在跑
    status = PROCESSING 且 agent_status = RUNNING → Agent 正在跑这一次执行

它承担的唯一职责是**并发互斥**：Agent 执行前必须先用一条原子 UPDATE 把
agent_status 从 IDLE 抢成 RUNNING（UPDATE ... WHERE agent_status = 'IDLE'），
只有 affected_rows = 1 的请求才有资格创建 AgentRun。这样"同一个工单被并发点 10 次"
也只会有一次真正执行，而不同工单之间没有任何共享锁，天然并发。

agent_run_id 记录"当前占据执行权的那一次 AgentRun"，用于：
    1. 抢占失败时告诉调用方"现在跑的是哪一次"；
    2. 判定超时接管时确认抢占的仍然是同一次执行。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class TicketStatus:
    OPEN = "OPEN"                    # 待处理
    PROCESSING = "PROCESSING"        # 处理中（Agent 或客服）
    WAITING_HUMAN = "WAITING_HUMAN"  # 等待人工确认/接管
    RESOLVED = "RESOLVED"            # 已解决（等用户确认）
    CLOSED = "CLOSED"                # 已关闭
    FAILED = "FAILED"                # 处理失败


class TicketType:
    ORDER = "ORDER"            # 订单问题
    LOGISTICS = "LOGISTICS"    # 物流问题
    REFUND = "REFUND"          # 退款
    AFTER_SALE = "AFTER_SALE"  # 售后其他
    OTHER = "OTHER"

    ALL = (ORDER, LOGISTICS, REFUND, AFTER_SALE, OTHER)


class AgentStatus:
    """工单上的 Agent 执行权状态（并发互斥用，不是业务状态）。

        IDLE          当前没有 Agent 在执行 —— 可以被抢占
        RUNNING       Agent 正在执行（或人工审批后正在恢复执行）—— 别人抢不到
        WAITING_HUMAN 上一次执行停在人工确认点 —— 只能由审批接口恢复，不能另起一次
    """

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"

    ALL = (IDLE, RUNNING, WAITING_HUMAN)
    #: 仍然"占着"工单的状态：处于这些状态时不允许创建新的 AgentRun
    OCCUPIED = (RUNNING, WAITING_HUMAN)


class TicketPriority:
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_sale_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default=TicketPriority.NORMAL)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TicketStatus.OPEN)

    # ---- Agent 执行权（并发互斥）----
    # 注意：这两列不参与业务状态流转，只回答"有没有 Agent 正在占用这张工单"。
    agent_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AgentStatus.IDLE, server_default=AgentStatus.IDLE
    )
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 客服领取后写入
    assignee_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_tickets_user_id", "user_id"),
        Index("idx_tickets_status", "status"),
        Index("idx_tickets_assignee_id", "assignee_id"),
        Index("idx_tickets_order_id", "order_id"),
        # 抢占语句走的是 `WHERE id = ? AND agent_status = 'IDLE'`，
        # 主键已经能定位到唯一一行，这个索引只是给"扫描卡住的工单"这类运维查询用。
        Index("idx_tickets_agent_status", "agent_status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_no": self.ticket_no,
            "user_id": self.user_id,
            "order_id": self.order_id,
            "after_sale_id": self.after_sale_id,
            "title": self.title,
            "content": self.content,
            "type": self.type,
            "priority": self.priority,
            "status": self.status,
            "agent_status": self.agent_status,
            "agent_run_id": self.agent_run_id,
            "assignee_id": self.assignee_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "closed_at": self.closed_at,
        }

    def __repr__(self) -> str:
        return (
            f"<Ticket id={self.id} no={self.ticket_no!r} status={self.status} "
            f"agent_status={self.agent_status}>"
        )
