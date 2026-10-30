"""持久化 Agent 任务与执行历史。

提交在工单行锁内创建任务并绑定指针；worker 通过本表状态和租约领取任务。
同一逻辑任务恢复时保留 run/thread，execution_epoch 区分每次领取的执行资格。
review_payload 保存审核方案及订单快照，approval_decision 保存不可反转的本轮决定。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class AgentRunStatus:
    """一次 Agent 执行的状态。

    取值（与需求/接口契约对齐）：
        PENDING        已创建，还没开始跑
        RUNNING        正在执行（含"从断点续跑"的过程）
        WAITING_HUMAN  停在人工确认点，等客服批准/驳回（执行权仍占着工单）
        COMPLETED      跑完了（含人工批准后恢复跑完）
        FAILED         失败（可恢复异常重试后仍失败，或不可恢复异常直接失败）
        REJECTED       人工驳回，交回客服人工处理

    历史说明：RUNNING 早期叫 PROCESSING（和工单的 PROCESSING 重名，容易混）。
    存量数据由 core/schema.py 的 sync_schema() 在启动时回填成 RUNNING。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"

    #: 终态：这些状态下这次执行已经结束，不再占用工单的执行权
    TERMINAL = (COMPLETED, FAILED, REJECTED)
    #: 活跃态：这次执行还"活着"（要么在跑，要么停在人工确认点等审批）
    ACTIVE = (PENDING, RUNNING, WAITING_HUMAN)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # LangGraph 执行上下文（Checkpoint 用它恢复中断的图）
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # 任务状态以本表为准；tickets.agent_status 仅作为旧页面的兼容投影。
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    execution_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    review_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    flow_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AgentRunStatus.PENDING
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 活跃时间：事件和独立续租都会刷新；任务是否可回收以 lease_until 为准。
    alive_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 当前正在执行的节点名（由 LangGraph 回调写入；不跑在节点里时为空）
    current_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 这一次执行内部重试了几次（只对可恢复异常重试，不会无限重试业务异常）
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 断点恢复：本次执行中途失败过，但复用了同一个 thread_id 的 Checkpoint 继续跑
    resumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    used_rag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    used_tool: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    need_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_agent_runs_queue", "status", "next_attempt_at", "lease_until"),
        Index("idx_agent_runs_ticket_id", "ticket_id"),
        Index("idx_agent_runs_review_deadline", "status", "review_deadline_at"),
        # 唯一：thread_id 是一次执行的 LangGraph 上下文，天然只能指向一条 run。
        # 同工单任务互斥由提交事务保证；thread_id 唯一只保护执行上下文标识。
        UniqueConstraint("thread_id", name="uq_agent_runs_thread_id"),
        Index("idx_agent_runs_status", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "flow_version": self.flow_version,
            "execution_spec": self.execution_spec,
            "review_requested_at": self.review_requested_at,
            "review_deadline_at": self.review_deadline_at,
            "review_reminded_at": self.review_reminded_at,
            "worker_id": self.worker_id,
            "lease_until": self.lease_until,
            "execution_epoch": self.execution_epoch,
            "next_attempt_at": self.next_attempt_at,
            "review_round": self.review_round,
            "review_payload": self.review_payload,
            "approval_decision": self.approval_decision,
            "id": self.id,
            "ticket_id": self.ticket_id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "alive_at": self.alive_at,
            "current_node": self.current_node,
            "retry_count": self.retry_count,
            "resumed": self.resumed,
            "error": self.error,
            "used_rag": self.used_rag,
            "used_tool": self.used_tool,
            "need_human": self.need_human,
            "decision": self.decision,
            "result": self.result,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:
        return f"<AgentRun id={self.id} ticket={self.ticket_id} status={self.status}>"
