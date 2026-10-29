"""售后服务：售后单的创建 / 查询 / 撤销。

售后单（AfterSale）是业务对象，工单（Ticket）是客服处理载体（v2.md §14）。
退款的具体执行在 refund_service，这里只管"申请"这一层。
"""

from decimal import Decimal, InvalidOperation

from app.core.database import session_scope
from app.core.exceptions import (
    AfterSaleNotFoundError,
    DuplicateOperationError,
    InvalidStateError,
    OrderNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import gen_after_sale_no
from app.core.logging import get_logger
from app.models.after_sale import AfterSaleStatus, AfterSaleType
from app.models.order import OrderAfterSaleStatus, PaymentStatus
from app.repositories import after_sale_repository, order_repository, refund_repository

logger = get_logger(__name__)


def create_after_sale(
    *,
    user_id: int,
    order_id: int,
    type: str = AfterSaleType.REFUND,
    reason: str,
    amount: Decimal | float | None = None,
) -> dict:
    """用户申请售后。

    校验（v2.md §17 的前 5 条在这里，第 6 条金额规则在 refund_service）：
        1. 订单存在
        2. 订单属于当前用户
        3. 订单已支付
        4. 该订单没有进行中的售后（幂等）
        5. 申请金额合法（<= 订单金额）
    """
    if type not in AfterSaleType.ALL:
        raise ValidationError(f"非法售后类型：{type}")
    if type != AfterSaleType.REFUND:
        raise ValidationError("当前在线售后仅支持全额退款；退货、换货请提交工单由客服受理",
                              code="AFTER_SALE_TYPE_UNSUPPORTED")

    with session_scope() as db:
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if order.user_id != user_id:
            raise PermissionDeniedError("无权对他人的订单申请售后")
        if order.payment_status != PaymentStatus.PAID:
            raise InvalidStateError("订单未支付或已退款，无法申请售后")

        existing = after_sale_repository.find_active_by_order(db, order_id)
        if existing is not None:
            logger.info("重复售后申请被拦截：order_id=%s 已有 %s", order_id, existing["after_sale_no"])
            raise DuplicateOperationError(
                f"该订单已有进行中的售后（{existing['after_sale_no']}）",
                code="DUPLICATE_AFTER_SALE",
            )

        final_amount = _resolve_amount(order, amount)
        if type in (AfterSaleType.REFUND, AfterSaleType.RETURN) and final_amount != Decimal(str(order.total_amount)):
            raise ValidationError("当前仅支持订单全额退款", code="PARTIAL_REFUND_UNSUPPORTED")

        record = after_sale_repository.create(
            db,
            after_sale_no=gen_after_sale_no(),
            order_id=order_id,
            user_id=user_id,
            type=type,
            amount=final_amount,
            reason=reason,
            status=AfterSaleStatus.APPLYING,
        )

        order.after_sale_status = OrderAfterSaleStatus.APPLYING
        db.flush()

    logger.info(
        "创建售后：after_sale_no=%s order_id=%s type=%s 金额=%s",
        record["after_sale_no"],
        order_id,
        type,
        record["amount"],
    )
    return get_after_sale(record["id"], user_id=user_id)


def get_after_sale(after_sale_id: int, *, user_id: int | None = None) -> dict:
    """售后详情（含退款流水）。user_id 不为 None 时校验归属。"""
    with session_scope() as db:
        record = after_sale_repository.get_by_id(db, after_sale_id)
        if record is None:
            raise AfterSaleNotFoundError(f"售后单 {after_sale_id} 不存在")
        if user_id is not None and record["user_id"] != user_id:
            raise PermissionDeniedError("无权查看他人的售后单")

        refunds = refund_repository.list_by_after_sale(db, after_sale_id)

    return {**record, "refunds": refunds}


def list_after_sales(
    *, user_id: int | None = None, limit: int = 20, offset: int = 0, status: str | None = None, order_id: int | None = None
) -> dict:
    """售后单列表：**一个方法服务两种视角**。

        user_id 不为 None → 用户端的"我的售后"（强制按归属过滤）
        user_id 为 None   → 客服/管理端全量列表（可按状态、订单筛）

    为什么合成一个方法而不是两个：分页/装配/返回结构完全一样，
    唯一的区别是 WHERE 里有没有那个 user_id。分成两个必然会慢慢长歪。
    """
    with session_scope() as db:
        if user_id is not None:
            items, total = after_sale_repository.list_by_user(
                db, user_id=user_id, limit=limit, offset=offset
            )
        else:
            items, total = after_sale_repository.list_all(
                db, limit=limit, offset=offset, status=status, order_id=order_id
            )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def cancel_after_sale(*, after_sale_id: int, user_id: int, reason: str | None = None) -> dict:
    """用户撤销售后（已完成的不能撤）。"""
    with session_scope() as db:
        record = after_sale_repository.get_model(db, after_sale_id)
        if record is None:
            raise AfterSaleNotFoundError(f"售后单 {after_sale_id} 不存在")
        if record.user_id != user_id:
            raise PermissionDeniedError("无权操作他人的售后单")
        # 与退款保持相同的加锁顺序：订单先锁，随后重新读取售后最新状态。
        order = order_repository.get_model_for_update(db, record.order_id)
        db.refresh(record, with_for_update=True)
        if record.status not in (AfterSaleStatus.APPLYING, AfterSaleStatus.PROCESSING):
            raise InvalidStateError(f"当前售后状态为 {record.status}，不能撤销")

        if reason:
            record.reason = f"{record.reason or ''}｜撤销原因：{reason}"

        record.status = AfterSaleStatus.CANCELLED
        db.flush()

        if order is not None and order.after_sale_status == OrderAfterSaleStatus.APPLYING:
            order.after_sale_status = OrderAfterSaleStatus.NONE
            db.flush()

        db.refresh(record)
        result = record.to_dict()

    logger.info("撤销售后：after_sale_no=%s", result["after_sale_no"])
    return result


def _resolve_amount(order, amount: Decimal | float | None) -> Decimal:
    """申请金额：默认全额，必须 > 0 且 <= 订单金额。"""
    total = Decimal(str(order.total_amount))
    try:
        final = total if amount is None else Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("售后金额格式无效") from exc

    if not final.is_finite():
        raise ValidationError("售后金额必须是有限数值")
    if final <= 0:
        raise ValidationError("售后金额必须大于 0")
    if final > total:
        raise ValidationError(f"售后金额 {final} 超过订单金额 {total}")
    return final
