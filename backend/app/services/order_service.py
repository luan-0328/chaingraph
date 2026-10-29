"""订单服务 —— 项目里事务最重的地方。

关键设计（v2.md §11、§59、§61）：
    1. **下单是一个事务**：创建订单 + 写明细（带商品快照）+ 扣库存 + 清购物车，
       要么全成功，要么全回滚；
    2. **状态转换只在 Service**：Router 不允许直接改状态；
    3. **归属校验**：查订单/取消订单/确认收货都要校验 order.user_id == current_user.id；
    4. **行锁**：扣库存和改订单状态用 SELECT ... FOR UPDATE，防并发。
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.database import session_scope
from app.core.exceptions import (
    EmptyCartError,
    InsufficientStockError,
    InvalidStateError,
    OrderNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import gen_order_no
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.order import FulfillmentStatus, OrderAfterSaleStatus, PaymentStatus
from app.repositories import (
    cart_repository,
    logistics_repository,
    order_repository,
    payment_repository,
    product_repository,
    user_repository,
)
from app.services import product_service

logger = get_logger(__name__)


# ======================================================================
# 下单（事务）
# ======================================================================
def create_order_from_cart(*, user_id: int, item_ids: list[int] | None = None) -> dict:
    """从购物车结算下单。

    流程（v2.md §8.3）：
        取购物车行 → 校验归属 → 校验商品可购 → 计算金额 →
        建订单 → 写明细（快照）→ 扣库存 → 清购物车 → 提交
    """
    with session_scope() as db:
        user_repository.lock_cart_owner(db, user_id)
        cart_items = _resolve_cart_items(db, user_id, item_ids)
        if not cart_items:
            raise EmptyCartError("购物车为空，无法下单")

        total_amount = Decimal("0")
        order_items: list[dict] = []

        for item in sorted(cart_items, key=lambda row: row.product_id):
            quantity = item.quantity
            product = product_service.ensure_purchasable(db, item.product_id, quantity)

            # 扣库存（内部用的是行锁 + 再次校验，防并发超卖）
            if not product_repository.decrease_stock(db, item.product_id, quantity):
                raise InsufficientStockError(
                    f"商品「{product['name']}」库存不足，请调整数量后重试"
                )

            price = Decimal(str(product["price"]))
            subtotal = price * quantity
            total_amount += subtotal

            order_items.append(
                {
                    "product_id": item.product_id,
                    # 快照：商品以后改价/改名，历史订单不受影响（v2.md §9.2）
                    "product_name": product["name"],
                    "product_price": price,
                    "quantity": quantity,
                    "subtotal": subtotal,
                }
            )

        order = order_repository.create(
            db,
            order_no=gen_order_no(),
            user_id=user_id,
            total_amount=total_amount,
        )
        order_repository.add_items(db, order_id=order["id"], items=order_items)

        # 已结算的购物车行清掉
        cart_repository.remove_items(db, [item.id for item in cart_items])

    logger.info(
        "创建订单：order_no=%s user=%s 金额=%s 明细=%s 件",
        order["order_no"],
        user_id,
        total_amount,
        len(order_items),
    )
    return get_order_detail(order["id"], user_id=user_id)


def create_order_from_items(
    *, user_id: int, items: list[dict]
) -> dict:
    """直接按商品明细下单（Agent 工具"帮用户下单"这类场景用，跳过购物车）。"""
    with session_scope() as db:
        if not items:
            raise ValidationError("下单明细不能为空")

        total_amount = Decimal("0")
        order_items: list[dict] = []

        for entry in items:
            quantity = int(entry["quantity"])
            if quantity <= 0:
                raise ValidationError("购买数量必须大于 0")

            product = product_service.ensure_purchasable(db, entry["product_id"], quantity)
            if not product_repository.decrease_stock(db, entry["product_id"], quantity):
                raise InsufficientStockError(f"商品「{product['name']}」库存不足")

            price = Decimal(str(product["price"]))
            subtotal = price * quantity
            total_amount += subtotal
            order_items.append(
                {
                    "product_id": entry["product_id"],
                    "product_name": product["name"],
                    "product_price": price,
                    "quantity": quantity,
                    "subtotal": subtotal,
                }
            )

        order = order_repository.create(
            db, order_no=gen_order_no(), user_id=user_id, total_amount=total_amount
        )
        order_repository.add_items(db, order_id=order["id"], items=order_items)

    return get_order_detail(order["id"], user_id=user_id)


def _resolve_cart_items(db: Session, user_id: int, item_ids: list[int] | None) -> list:
    """取出要结算的购物车行；不传 item_ids 表示全选。"""
    if item_ids:
        rows = cart_repository.list_models_by_ids(db, user_id, item_ids)
        if len(rows) != len(set(item_ids)):
            raise ValidationError("部分购物车项不存在或不属于当前用户")
        return rows
    models = cart_repository.list_models_by_ids(
        db, user_id, [item["id"] for item in cart_repository.list_by_user(db, user_id)]
    )
    return models


# ======================================================================
# 查询
# ======================================================================
def list_my_orders(
    *,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
    payment_status: str | None = None,
    fulfillment_status: str | None = None,
) -> dict:
    """用户的"我的订单"列表。

    三个状态维度里只暴露两个筛选项（支付/履约）—— **售后状态没做成筛选项**：
    它由售后流程自动推进，用户想找"退款中的订单"用支付状态 REFUNDED 就够，
    多一个入口只会让前端筛选区变复杂。
    """
    with session_scope() as db:
        items, total = order_repository.list_by_user(
            db,
            user_id=user_id,
            limit=limit,
            offset=offset,
            payment_status=payment_status,
            fulfillment_status=fulfillment_status,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def list_all_orders(
    *,
    limit: int = 20,
    offset: int = 0,
    keyword: str | None = None,
    payment_status: str | None = None,
    fulfillment_status: str | None = None,
    user_id: int | None = None,
) -> dict:
    """客服/管理端：全部订单。"""
    with session_scope() as db:
        items, total = order_repository.list_all(
            db,
            limit=limit,
            offset=offset,
            keyword=keyword,
            payment_status=payment_status,
            fulfillment_status=fulfillment_status,
            user_id=user_id,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_order_detail(order_id: int, *, user_id: int | None = None) -> dict:
    """订单详情（含明细/支付/物流）。

    user_id 不为 None 时校验归属：用户视角只能看自己的订单；
    客服/管理端传 None（他们本来就要看全部订单）。
    """
    with session_scope() as db:
        order = order_repository.get_by_id(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if user_id is not None and order["user_id"] != user_id:
            raise PermissionDeniedError("无权查看他人的订单")

        items = order_repository.list_by_order(db, order_id)
        payments = payment_repository.list_by_order(db, order_id)
        logistics = logistics_repository.get_by_order_id(db, order_id)

    return {**order, "items": items, "payments": payments, "logistics": logistics}


def assert_order_owner(order: dict, user_id: int) -> None:
    """归属校验的复用函数（Agent 工具也会调它做二次确认）。"""
    if order["user_id"] != user_id:
        raise PermissionDeniedError("该订单不属于当前用户")


# ======================================================================
# 状态转换（只允许在这里改）
# ======================================================================
def cancel_order(*, order_id: int, user_id: int) -> dict:
    """用户取消订单：只有"未支付"可以取消，取消后回补库存。"""
    with session_scope() as db:
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if order.user_id != user_id:
            raise PermissionDeniedError("无权操作他人的订单")
        from app.core.execution import execution_claim
        claim = execution_claim.get()
        if claim is not None:
            from app.models.agent_run import AgentRun
            from app.services.agent_task_service import order_review_snapshot
            run = db.get(AgentRun, claim.run_id)
            if run.approval_decision and run.approval_decision.get("approved"):
                if (run.review_payload or {}).get("order_snapshot") != order_review_snapshot(db, order_id):
                    raise InvalidStateError("审批后订单状态已变化，请重新审核取消操作")
        if order.payment_status != PaymentStatus.UNPAID:
            raise InvalidStateError("订单已支付或已退款，不能直接取消（请走售后流程）")
        if order.fulfillment_status == FulfillmentStatus.CANCELLED:
            raise InvalidStateError("订单已经取消过了")

        order.fulfillment_status = FulfillmentStatus.CANCELLED
        db.flush()

        # 回补库存
        for item in order_repository.list_by_order(db, order_id):
            product_repository.increase_stock(db, item["product_id"], item["quantity"])

        db.refresh(order)
        result = order.to_dict()

    logger.info("取消订单：order_no=%s user=%s", result["order_no"], user_id)
    return result


def confirm_receipt(*, order_id: int, user_id: int) -> dict:
    """用户确认收货 → 完成。"""
    with session_scope() as db:
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if order.user_id != user_id:
            raise PermissionDeniedError("无权操作他人的订单")
        if order.fulfillment_status not in (
            FulfillmentStatus.SHIPPED,
            FulfillmentStatus.DELIVERED,
        ):
            raise InvalidStateError("订单还没发货，无法确认收货")

        order.fulfillment_status = FulfillmentStatus.COMPLETED
        order.completed_at = utcnow()
        db.flush()
        db.refresh(order)
        result = order.to_dict()

    logger.info("确认收货：order_no=%s", result["order_no"])
    return result
