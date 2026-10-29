"""支付数据访问。

本层只拼 SQL / flush，**绝不 commit**（事务边界在 Service，见 core/database.session_scope）。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.payment import ACTIVE_PAYMENT_STATUSES, Payment, PaymentRecordStatus


def create(
    db: Session,
    *,
    order_id: int,
    payment_no: str,
    amount: Decimal | float,
    status: str = PaymentRecordStatus.PENDING,
) -> dict:
    """新建一条支付流水。

    默认落 PENDING 而不是直接 SUCCESS：真实支付有"发起 → 渠道确认"两段，
    PENDING 期间它已经占住订单（见 models/payment.py 的生成列），
    所以并发/重试也插不进第二笔。模拟支付会在同一个事务里紧跟着调 claim_success。

    amount 走 `Decimal(str(...))`：先转字符串再转 Decimal，
    避免 float 的二进制误差直接被塞进钱字段（0.1 存成 0.1000000000000000055 那种）。
    """
    payment = Payment(
        order_id=order_id,
        payment_no=payment_no,
        amount=Decimal(str(amount)),
        status=status,
    )
    db.add(payment)
    db.flush()
    db.refresh(payment)
    return payment.to_dict()


def get_by_id(db: Session, payment_id: int) -> dict | None:
    """按主键取支付流水（字典形式）。"""
    payment = db.get(Payment, payment_id)
    return payment.to_dict() if payment else None


def get_success_by_order(db: Session, order_id: int) -> dict | None:
    """查这笔订单是否已有成功支付（幂等：重复支付要拦住）。"""
    payment = db.scalar(
        select(Payment)
        .where(Payment.order_id == order_id, Payment.status == PaymentRecordStatus.SUCCESS)
        .order_by(Payment.id.desc())
    )
    return payment.to_dict() if payment else None


def get_pending_by_order(db: Session, order_id: int) -> dict | None:
    """这笔订单有没有"正在支付中"的流水。

    当前模拟支付是同步的（PENDING → SUCCESS 在同一个事务里），所以正常路径下
    它几乎总是空。保留它是为了**将来接真实网关**（异步回调）时幂等逻辑不用重写：
    那时"支付中"会真实存在一段时间，必须能查到并挡住第二笔。
    """
    payment = db.scalar(
        select(Payment)
        .where(Payment.order_id == order_id, Payment.status == PaymentRecordStatus.PENDING)
        .order_by(Payment.id.desc())
    )
    return payment.to_dict() if payment else None


def claim_success(db: Session, payment_id: int, paid_at: datetime) -> dict | None:
    """**条件更新**：把这一笔支付从 PENDING 抢成 SUCCESS，抢到才返回它。

        UPDATE payments SET status='SUCCESS', paid_at=?
         WHERE id=? AND status='PENDING'

    这是"重复支付只能成功一次"里最硬的一层：
    调用方必须拿 affected_rows 当判据 —— 拿到行的那一个请求才允许去改订单状态，
    没拿到的说明这笔流水已经被别人（或更早的请求）推进过了。

    为什么不能"先查再改"：两个并发请求都会查到 PENDING，然后都去改订单；
    `WHERE status='PENDING'` 是数据库在单条语句里完成的"检查 + 修改"，
    没有中间的竞态窗口。
    """
    result = db.execute(
        update(Payment)
        .where(
            Payment.id == payment_id,
            Payment.status == PaymentRecordStatus.PENDING,
        )
        .values(status=PaymentRecordStatus.SUCCESS, paid_at=paid_at)
    )
    if result.rowcount != 1:
        db.flush()
        return None
    db.flush()
    payment = db.get(Payment, payment_id)
    return payment.to_dict() if payment is not None else None


def find_active_by_order(db: Session, order_id: int) -> dict | None:
    """这笔订单当前有没有"有效支付"（PENDING/SUCCESS）。

    和 get_success_by_order 的区别：把"正在支付中"也算上 ——
    那些最终会变成 SUCCESS，必须先挡住，否则会付出两次钱。
    """
    payment = db.scalar(
        select(Payment)
        .where(
            Payment.order_id == order_id,
            Payment.status.in_(ACTIVE_PAYMENT_STATUSES),
        )
        .order_by(Payment.id.desc())
    )
    return payment.to_dict() if payment else None


def mark_failed(db: Session, payment_id: int) -> dict | None:
    """把一笔支付标成 FAILED（网关失败/超时）。

    标成 FAILED 后 `active_order_id` 生成列变成 NULL，订单**重新可以支付** ——
    "失败不占位"就是靠这个生成列实现的（见 models/payment.py）。
    """
    payment = db.get(Payment, payment_id)
    if payment is None:
        return None
    payment.status = PaymentRecordStatus.FAILED
    db.flush()
    db.refresh(payment)
    return payment.to_dict()


def list_by_order(db: Session, order_id: int) -> list[dict]:
    """某订单的全部支付流水（正序）—— 订单详情页展示"支付记录"用。

    正常情况下只有一条 SUCCESS；返回列表是为了把失败尝试也如实展示出来
    （用户看到"失败过一次、后来成功了"比只看到一条成功更不困惑）。
    注意：**不要**拿它来判断"能不能支付"，那要用 find_active_by_order。
    """
    rows = db.scalars(
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.asc())
    )
    return [row.to_dict() for row in rows]
