"""退款记录（v2.md §16）。

和 AfterSale 的区别：
    AfterSale      "用户想要退款"这件事的申请单
    RefundRecord   "钱真的退回去了"这笔资金流水
一次售后可能产生多次退款尝试（失败重试），所以拆成两张表。

"一个订单只能退一次"由什么保证（和 payments 完全同构，见 models/payment.py）
--------------------------------------------------------------------------
    ① Service 条件更新   refund_repository.claim_success()
                        `UPDATE refund_records SET status='SUCCESS'
                         WHERE id=? AND status='PENDING'`
                        只有 affected_rows=1 的请求才继续往下改订单/售后/库存。
    ② 订单行锁 + 事务内复查   refund_service._execute() 用 `SELECT ... FOR UPDATE`
                        拿锁后按**已提交的最终状态**重查一遍（本项目的退款是
                        同步即时到账，所以前一笔提交时已经是 SUCCESS 而不是 PENDING）。
    ③ **数据库唯一约束**  active_order_id 生成列 + uq_refund_records_active_order：
                        同一订单只允许存在一条"进行中或已成功"的退款。
                        FAILED 的退款尝试不占位 —— 失败后重试是正常业务。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, money


class RefundStatus:
    PENDING = "PENDING"      # 退款处理中（等待执行/等待审批）
    SUCCESS = "SUCCESS"      # 退款成功
    FAILED = "FAILED"        # 退款失败


#: 哪些退款状态算"占住了这笔钱"。PENDING 必须算：
#: "有进行中的退款"正是重复退款最典型的形态。
ACTIVE_REFUND_STATUSES = ("PENDING", "SUCCESS")


class RefundRecord(Base, TimestampMixin):
    __tablename__ = "refund_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refund_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    after_sale_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RefundStatus.PENDING)

    # 谁批准的：NULL=系统自动，其它=客服/管理员 user_id
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: 生成列："占住这笔钱"的退款才等于 order_id，否则 NULL（详见模块 docstring）
    active_order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN status IN ('" + "','".join(ACTIVE_REFUND_STATUSES) + "') "
            "THEN order_id ELSE NULL END",
            persisted=True,
        ),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_refund_records_after_sale_id", "after_sale_id"),
        Index("idx_refund_records_order_id", "order_id"),
        Index("idx_refund_records_status", "status"),
        # 一个订单至多一笔"进行中/已成功"的退款（数据库层兜底）
        UniqueConstraint("active_order_id", name="uq_refund_records_active_order"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "refund_no": self.refund_no,
            "after_sale_id": self.after_sale_id,
            "order_id": self.order_id,
            "user_id": self.user_id,
            "amount": money(self.amount),
            "reason": self.reason,
            "status": self.status,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    def __repr__(self) -> str:
        return f"<RefundRecord id={self.id} no={self.refund_no!r} status={self.status}>"
