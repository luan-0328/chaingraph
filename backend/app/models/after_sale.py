"""售后单（v2.md §15）。

区分清楚（这是 v2 的核心概念之一）：
    AfterSale 是**业务对象**（退款/退货/换货的业务单据）
    Ticket    是**客服处理载体**（沟通与处理过程）
一个售后单可以关联多个工单；工单也可以不关联售后（纯咨询）。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, money


class AfterSaleType:
    REFUND = "REFUND"      # V1 核心实现
    RETURN = "RETURN"      # 退货退款（保留结构）
    EXCHANGE = "EXCHANGE"  # 换货（保留结构）
    OTHER = "OTHER"

    ALL = (REFUND, RETURN, EXCHANGE, OTHER)


class AfterSaleStatus:
    APPLYING = "APPLYING"      # 用户已申请
    PROCESSING = "PROCESSING"  # 处理中
    APPROVED = "APPROVED"      # 已通过（待执行）
    REJECTED = "REJECTED"      # 已拒绝
    COMPLETED = "COMPLETED"    # 已完成（退款到账）
    CANCELLED = "CANCELLED"    # 用户撤销


class AfterSale(Base, TimestampMixin):
    __tablename__ = "after_sales"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    after_sale_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default=AfterSaleType.REFUND)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AfterSaleStatus.APPLYING
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_after_sales_order_id", "order_id"),
        Index("idx_after_sales_user_id", "user_id"),
        Index("idx_after_sales_status", "status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "after_sale_no": self.after_sale_no,
            "order_id": self.order_id,
            "user_id": self.user_id,
            "type": self.type,
            "reason": self.reason,
            "status": self.status,
            "amount": money(self.amount),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    def __repr__(self) -> str:
        return f"<AfterSale id={self.id} no={self.after_sale_no!r} status={self.status}>"
