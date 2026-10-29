"""工单消息（v2.md §20）。

用户与客服/Agent 的沟通都挂在这张表上 —— 不是另做一个"聊天系统"，
而是工单的对话记录，天然带业务上下文。
"""

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class SenderType:
    USER = "USER"
    AGENT = "AGENT"
    CUSTOMER_SERVICE = "CUSTOMER_SERVICE"
    SYSTEM = "SYSTEM"


class TicketMessage(Base, TimestampMixin):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_type: Mapped[str] = mapped_column(String(24), nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(191), nullable=True)

    __table_args__ = (Index("idx_ticket_messages_ticket_id", "ticket_id"),
                     Index("uq_ticket_messages_dedup", "dedup_key", unique=True))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "sender_type": self.sender_type,
            "sender_id": self.sender_id,
            "content": self.content,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:
        return f"<TicketMessage ticket={self.ticket_id} from={self.sender_type}>"
