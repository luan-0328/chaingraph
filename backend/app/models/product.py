"""商品表（v2.md §7.2）。

库存是"钱和货"里最容易被并发搞坏的一处，所以做了两层保护：
    1. 扣减用单条条件 UPDATE（`WHERE stock >= ?`，见 product_repository.decrease_stock）；
    2. 数据库再加一条 CHECK 约束，任何绕过应用层的写入也别想把库存弄成负数。
"""

from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, money


class ProductStatus:
    ON_SALE = "ON_SALE"
    OFF_SALE = "OFF_SALE"


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProductStatus.ON_SALE
    )

    __table_args__ = (
        Index("idx_products_status", "status"),
        Index("idx_products_name", "name"),
        # 最后一道防线：应用层再怎么写错，库存也不会变成负数（MySQL 8.0.16+ 真正生效）
        CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "price": money(self.price),
            "stock": self.stock,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:
        return f"<Product id={self.id} name={self.name!r} stock={self.stock}>"
