"""订单（v2.md §9.1、§10）。

关键设计：**三个状态字段分开，不要塞进一个 status**
    payment_status      UNPAID / PAID / REFUNDED
    fulfillment_status  PENDING / PROCESSING / SHIPPED / DELIVERED / COMPLETED / CANCELLED
    after_sale_status   NONE / APPLYING / REFUNDING / REFUNDED
状态转换统一由 OrderService 控制（v2.md §11：禁止 Controller 直接改状态）。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, money


class PaymentStatus:
    UNPAID = "UNPAID"
    PAID = "PAID"
    REFUNDED = "REFUNDED"


class FulfillmentStatus:
    PENDING = "PENDING"          # 已支付待发货
    PROCESSING = "PROCESSING"    # 备货中
    SHIPPED = "SHIPPED"          # 已发货
    DELIVERED = "DELIVERED"      # 已签收
    COMPLETED = "COMPLETED"      # 已完成（用户确认收货）
    CANCELLED = "CANCELLED"      # 已取消


class OrderAfterSaleStatus:
    NONE = "NONE"
    APPLYING = "APPLYING"
    REFUNDING = "REFUNDING"
    REFUNDED = "REFUNDED"


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    payment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaymentStatus.UNPAID
    )
    fulfillment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FulfillmentStatus.PENDING
    )
    after_sale_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderAfterSaleStatus.NONE
    )

    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_orders_user_id", "user_id"),
        Index("idx_orders_payment_status", "payment_status"),
        Index("idx_orders_fulfillment_status", "fulfillment_status"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_no": self.order_no,
            "user_id": self.user_id,
            "total_amount": money(self.total_amount),
            "payment_status": self.payment_status,
            "fulfillment_status": self.fulfillment_status,
            "after_sale_status": self.after_sale_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "paid_at": self.paid_at,
            "shipped_at": self.shipped_at,
            "completed_at": self.completed_at,
        }

    def __repr__(self) -> str:
        return f"<Order id={self.id} no={self.order_no!r} pay={self.payment_status} ship={self.fulfillment_status}>"
