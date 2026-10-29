"""订单明细（v2.md §9.2）。

**必须保存下单时的商品快照**（product_name / product_price）：
商品改价或下架后，历史订单展示的仍然是当时的价格与名称，
不能永远依赖 Product 当前值 —— 否则用户看到的订单金额会"自己变"。
"""

from decimal import Decimal

from sqlalchemy import BigInteger, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import money


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ---- 下单时的快照 ----
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    product_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    __table_args__ = (Index("idx_order_items_order_id", "order_id"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "product_price": money(self.product_price),
            "quantity": self.quantity,
            "subtotal": money(self.subtotal),
        }

    def __repr__(self) -> str:
        return f"<OrderItem order={self.order_id} product={self.product_name!r} x{self.quantity}>"
