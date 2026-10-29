"""物流（v2.md §13.1）。模拟物流数据，一个订单一条。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class LogisticsStatus:
    PENDING = "PENDING"          # 已揽收/待发出
    IN_TRANSIT = "IN_TRANSIT"    # 运输中
    DELIVERED = "DELIVERED"      # 已签收


class Logistics(Base, TimestampMixin):
    __tablename__ = "logistics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    company: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LogisticsStatus.PENDING
    )
    latest_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 最近一次物流更新（Agent 用它判断"物流是否停滞"，见 v2.md 知识库规则）
    last_update_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_logistics_order_id", "order_id"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "tracking_no": self.tracking_no,
            "company": self.company,
            "status": self.status,
            "latest_message": self.latest_message,
            "last_update_time": self.last_update_time,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:
        return f"<Logistics order={self.order_id} no={self.tracking_no!r} status={self.status}>"
