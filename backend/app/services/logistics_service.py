"""物流服务：发货 / 运输流转 / 签收（模拟物流）。

角色分工（v2.md §13.2）：
    客服 / 管理员：确认发货（生成运单号、订单转 SHIPPED）
    系统（模拟）：运输中 / 已签收
"""

from app.core.database import session_scope
from app.core.exceptions import (
    InvalidStateError,
    LogisticsNotFoundError,
    OrderNotFoundError,
    PermissionDeniedError,
)
from app.core.ids import gen_tracking_no
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.logistics import LogisticsStatus
from app.models.order import FulfillmentStatus, PaymentStatus
from app.repositories import logistics_repository, order_repository

logger = get_logger(__name__)

DEFAULT_COMPANY = "顺丰速运"


def ship_order(*, order_id: int, operator_id: int) -> dict:
    """确认发货：创建物流记录 + 订单转 SHIPPED。"""
    with session_scope() as db:
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if order.payment_status != PaymentStatus.PAID:
            raise InvalidStateError("订单未支付，不能发货")
        if order.fulfillment_status in (
            FulfillmentStatus.SHIPPED,
            FulfillmentStatus.DELIVERED,
            FulfillmentStatus.COMPLETED,
        ):
            raise InvalidStateError(f"订单当前履约状态为 {order.fulfillment_status}，不能重复发货")
        if order.fulfillment_status == FulfillmentStatus.CANCELLED:
            raise InvalidStateError("订单已取消，不能发货")

        existing = logistics_repository.get_by_order_id(db, order_id)
        if existing is not None:
            raise InvalidStateError("该订单已有物流记录，不能重复发货")

        record = logistics_repository.create(
            db,
            order_id=order_id,
            tracking_no=gen_tracking_no(),
            company=DEFAULT_COMPANY,
            status=LogisticsStatus.PENDING,
            latest_message="商家已发货，等待揽收",
            last_update_time=utcnow(),
        )

        order.fulfillment_status = FulfillmentStatus.SHIPPED
        order.shipped_at = utcnow()
        db.flush()
        db.refresh(order)
        order_dict = order.to_dict()

    logger.info(
        "订单发货：order_no=%s tracking_no=%s operator=%s",
        order_dict["order_no"],
        record["tracking_no"],
        operator_id,
    )
    return {"order": order_dict, "logistics": record}


def update_transit(*, order_id: int, message: str) -> dict:
    """模拟物流推进：运输中。"""
    with session_scope() as db:
        record = logistics_repository.get_model_by_order(db, order_id)
        if record is None:
            raise LogisticsNotFoundError(f"订单 {order_id} 还没有物流记录")

        record.status = LogisticsStatus.IN_TRANSIT
        record.latest_message = message
        record.last_update_time = utcnow()
        db.flush()
        db.refresh(record)
        result = record.to_dict()

    logger.info("物流更新：order_id=%s → IN_TRANSIT (%s)", order_id, message)
    return result


def deliver_order(*, order_id: int) -> dict:
    """模拟签收：物流 DELIVERED + 订单 DELIVERED（等用户确认收货转 COMPLETED）。"""
    with session_scope() as db:
        record = logistics_repository.get_model_by_order(db, order_id)
        if record is None:
            raise LogisticsNotFoundError(f"订单 {order_id} 还没有物流记录")

        record.status = LogisticsStatus.DELIVERED
        record.latest_message = "快件已签收"
        record.last_update_time = utcnow()
        db.flush()

        order = order_repository.get_model_for_update(db, order_id)
        if order is not None and order.fulfillment_status == FulfillmentStatus.SHIPPED:
            order.fulfillment_status = FulfillmentStatus.DELIVERED
            db.flush()
            db.refresh(order)

        db.refresh(record)
        result = {"logistics": record.to_dict(), "order": order.to_dict() if order else None}

    logger.info("快件签收：order_id=%s", order_id)
    return result


def get_logistics(order_id: int, *, user_id: int | None = None) -> dict:
    """查某订单的物流。

    两种"查不到"要分清（它们的语义完全不同）：

        订单不属于你     → 403 PermissionDeniedError（越权）
        订单是你的但没物流 → 404 LogisticsNotFoundError（还没发货，正常状态）

    混成一个错误的话，用户会以为"我的订单出问题了"，
    而实际上只是商家还没发货 —— 前端要把这两句话说得不一样。

    user_id 不为 None 时校验归属；客服/管理端传 None。
    """
    with session_scope() as db:
        order = order_repository.get_by_id(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if user_id is not None and order["user_id"] != user_id:
            raise PermissionDeniedError("无权查看他人订单的物流")

        record = logistics_repository.get_by_order_id(db, order_id)

    if record is None:
        raise LogisticsNotFoundError(f"订单 {order_id} 还没有物流信息（可能还未发货）")
    return record
