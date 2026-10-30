"""Agent 执行事件（v2.md §43）。

客服工作台要展示"业务过程"，而不是模型思维链：
    STARTED → ANALYZING → QUERY_ORDER → RETRIEVING_KNOWLEDGE → TOOL_CALLING
    → EXECUTING_REFUND → WAITING_HUMAN → COMPLETED / FAILED

事件既落库（工作台/审计），也通过 SSE 实时推给前端 —— 一份数据两个出口。
"""

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class AgentEventType:
    STARTED = "STARTED"
    ANALYZING = "ANALYZING"
    DECIDED = "DECIDED"
    QUERY_ORDER = "QUERY_ORDER"
    QUERY_LOGISTICS = "QUERY_LOGISTICS"
    QUERY_AFTER_SALE = "QUERY_AFTER_SALE"
    RETRIEVING_KNOWLEDGE = "RETRIEVING_KNOWLEDGE"
    TOOL_CALLING = "TOOL_CALLING"
    EXECUTING_REFUND = "EXECUTING_REFUND"
    RESULT_GENERATED = "RESULT_GENERATED"
    WAITING_HUMAN = "WAITING_HUMAN"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    REVIEW_REMINDER = "REVIEW_REMINDER"
    REVIEW_EXPIRED = "REVIEW_EXPIRED"
    # 可恢复异常（LLM 超时 / 429 / 依赖抖动）触发的重试，也如实记进过程流
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentRunEvent(Base, TimestampMixin):
    __tablename__ = "agent_run_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(191), nullable=True)

    __table_args__ = (Index("idx_agent_run_events_run_id", "agent_run_id"),
                     Index("uq_agent_events_dedup", "dedup_key", unique=True))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_run_id": self.agent_run_id,
            "event_type": self.event_type,
            "message": self.message,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:
        return f"<AgentRunEvent run={self.agent_run_id} type={self.event_type}>"
