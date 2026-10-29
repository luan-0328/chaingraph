"""支付记录（v2.md §12.1）。不接真实支付，但按真实流程建模。

"一个订单只能有一笔有效支付"由什么保证
--------------------------------------
分三层，越往下越硬：

    ① Service 条件更新   payment_repository.claim_success()
                        `UPDATE payments SET status='SUCCESS' WHERE id=? AND status='PENDING'`
                        只有 affected_rows=1 的那个请求才算"这笔支付由我完成"。
    ② 订单行锁 + 事务内复查   pay_order() 拿 `SELECT ... FOR UPDATE` 再查一次订单状态。
    ③ **数据库唯一约束**  active_order_id 生成列 + uq_payments_active_order：
                        同一个 order_id 只允许存在一条"有效"（PENDING/SUCCESS）支付。
                        哪怕前两层因为任何原因被绕过，第二条 INSERT 也会撞唯一键。

为什么用生成列而不是普通唯一索引
    失败的支付尝试（FAILED）必须允许重复 —— 用户重试支付是正常业务。
    普通 `UNIQUE(order_id)` 会把"第一次支付失败"变成"永远不能再支付"。
    生成列在 status 不是有效状态时取 NULL，而 MySQL 唯一索引**不约束 NULL**，
    于是"有效支付至多一条、失败支付不限条数"正好被一条索引表达出来。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Computed, DateTime, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, money

#: 哪些支付状态算"有效"（占住这个订单的钱）。
#: PENDING 也算：模拟支付在同一个事务里 PENDING→SUCCESS，但真要异步化时，
#: "正在支付中"同样必须挡住第二笔，否则会付出两次钱。
ACTIVE_PAYMENT_STATUSES = ("PENDING", "SUCCESS")


class PaymentRecordStatus:
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payment_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PaymentRecordStatus.PENDING
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: 生成列："有效支付"才等于 order_id，否则 NULL（详见模块 docstring）。
    #: 由 MySQL 自己维护，应用层永远不写它 —— 也就永远不会和 status 脱节。
    active_order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN status IN ('" + "','".join(ACTIVE_PAYMENT_STATUSES) + "') "
            "THEN order_id ELSE NULL END",
            persisted=True,
        ),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_payments_order_id", "order_id"),
        # 一个订单至多一笔有效支付（数据库层兜底，不只是"Service 记得查一下"）
        UniqueConstraint("active_order_id", name="uq_payments_active_order"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "payment_no": self.payment_no,
            "amount": money(self.amount),
            "status": self.status,
            "paid_at": self.paid_at,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:
        return f"<Payment id={self.id} no={self.payment_no!r} status={self.status}>"
