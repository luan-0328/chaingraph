"""每个查看者独立的已读游标，客服之间不共享已读状态。"""

from sqlalchemy import BigInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class TicketRead(Base, TimestampMixin):
    __tablename__ = "ticket_reads"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    __table_args__ = (UniqueConstraint("ticket_id", "user_id", name="uq_ticket_reads_viewer"),)
