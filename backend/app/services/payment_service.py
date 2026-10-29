"""支付服务（模拟支付，但按真实流程做状态机 + 幂等 + 事务）。

v2.md §12.2 的流程：
    校验归属 → 校验状态 → 创建 Payment → 模拟支付 → Payment SUCCESS
    → Order.payment_status = PAID → Order.fulfillment_status = PENDING

幂等（v2.md §60）：同一订单重复点击支付，不能产生第二笔成功支付。

"只成功一次"靠三层，缺一层都能被并发绕过（越往下越硬）
------------------------------------------------------
    ① 订单行锁：`SELECT ... FOR UPDATE`（pay_order 进来先拿锁）
    ② **条件更新**：`UPDATE orders SET payment_status='PAID'
                     WHERE id=? AND payment_status='UNPAID'`
                    以及 `UPDATE payments SET status='SUCCESS'
                     WHERE id=? AND status='PENDING'`
       只有 affected_rows=1 的请求才算"这笔钱是我收的"，拿不到就回滚。
    ③ 数据库唯一约束：payments.active_order_id + uq_payments_active_order
       （PENDING/SUCCESS 才占位）—— 哪怕前两层全被绕过，也插不进第二条有效支付。

为什么"先查再改"不够：两个并发请求都会查到 UNPAID，然后都去创建流水。
行锁保证了同一时刻只有一个能进来，条件更新保证了"进来的那个确实改到了状态"，
唯一索引则是最后一道谁都不信的物理防线。
"""

from app.core.database import session_scope
from app.core.exceptions import (
    DuplicateOperationError,
    InvalidStateError,
    OrderNotFoundError,
    PermissionDeniedError,
)
from app.core.ids import gen_payment_no
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.order import FulfillmentStatus, PaymentStatus
from app.repositories import order_repository, payment_repository

logger = get_logger(__name__)


def pay_order(*, order_id: int, user_id: int) -> dict:
    """支付订单。返回 {order, payment}。"""
    with session_scope() as db:
        # 加锁读订单：并发下只有一个请求能进来改状态
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if order.user_id != user_id:
            raise PermissionDeniedError("无权支付他人的订单")

        # ---- 幂等保护 1：已经支付成功过 ----
        existing = payment_repository.get_success_by_order(db, order_id)
        if existing is not None or order.payment_status == PaymentStatus.PAID:
            logger.info("重复支付被拦截：order_no=%s", order.order_no)
            raise DuplicateOperationError("该订单已支付，无需重复支付", code="ALREADY_PAID")

        # ---- 幂等保护 2：还有一笔"支付中"的流水没走完 ----
        # SUCCESS 和 PENDING 都算占用（见 models/payment.py），
        # 所以这里要连 PENDING 一起拦，不能只看 SUCCESS。
        inflight = payment_repository.find_active_by_order(db, order_id)
        if inflight is not None:
            raise DuplicateOperationError(
                f"该订单已有进行中的支付（{inflight['payment_no']}）", code="PAYMENT_IN_PROGRESS"
            )

        if order.payment_status != PaymentStatus.UNPAID:
            raise InvalidStateError(f"订单当前支付状态为 {order.payment_status}，无法支付")
        if order.fulfillment_status == FulfillmentStatus.CANCELLED:
            raise InvalidStateError("订单已取消，无法支付")

        # ---- 模拟支付：创建流水 → 立即置成功 ----
        payment = payment_repository.create(
            db,
            order_id=order_id,
            payment_no=gen_payment_no(),
            amount=order.total_amount,
            status="PENDING",
        )

        # ---- 条件更新 ①：这一笔流水由我推进到 SUCCESS ----
        claimed = payment_repository.claim_success(db, payment["id"], utcnow())
        if claimed is None:
            # 拿不到说明这笔流水已经不是 PENDING —— 绝不继续改订单
            raise DuplicateOperationError("该支付流水已被处理，本次未重复执行", code="ALREADY_PAID")
        payment = claimed

        # ---- 条件更新 ②：订单由 UNPAID → PAID（同一个事务）----
        if not order_repository.claim_paid(db, order_id, utcnow()):
            raise DuplicateOperationError("订单状态已被其他支付请求变更，本次支付未生效", code="ALREADY_PAID")

        # 上面走的是 UPDATE 语句，ORM 里的对象可能过期 → 先失效再读，拿到库里的真值
        db.expire(order)
        db.refresh(order)
        order_dict = order.to_dict()

    logger.info(
        "支付成功：order_no=%s payment_no=%s 金额=%s",
        order_dict["order_no"],
        payment["payment_no"],
        payment["amount"],
    )
    return {"order": order_dict, "payment": payment}


def list_payments(order_id: int, *, user_id: int | None = None) -> list[dict]:
    """某订单的支付流水（订单详情页展示）。

    user_id 不为 None 时校验归属：普通用户只能看自己订单的支付记录，
    否则改一个 order_id 就能窥探别人的交易金额与时间。
    客服/管理端传 None。
    """
    with session_scope() as db:
        if user_id is not None:
            order = order_repository.get_by_id(db, order_id)
            if order is None:
                raise OrderNotFoundError(f"订单 {order_id} 不存在")
            if order["user_id"] != user_id:
                raise PermissionDeniedError("无权查看他人订单的支付记录")
        return payment_repository.list_by_order(db, order_id)
