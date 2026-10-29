"""订单接口（v2.md §45 Order）。"""

from fastapi import APIRouter, Depends, Query, status

from app.api import mappers
from app.core.dependencies import get_current_user
from app.core.logging import get_logger
from app.schemas.order import (
    LogisticsInfo,
    OrderCreateRequest,
    OrderDetail,
    OrderListResponse,
    OrderSummary,
)
from app.services import logistics_service, order_service, payment_service

logger = get_logger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", response_model=OrderDetail, status_code=status.HTTP_201_CREATED, summary="结算下单")
def create_order(
    payload: OrderCreateRequest,
    current_user: dict = Depends(get_current_user),
) -> OrderDetail:
    """从购物车结算下单（不传 item_ids = 结算全部）。

    这是**一个事务**：建订单 + 写明细 + 扣库存 + 清购物车，任何一步失败全部回滚。
    """
    order = order_service.create_order_from_cart(
        user_id=current_user["id"], item_ids=payload.item_ids
    )
    return OrderDetail(**mappers.order_detail(order))


@router.get("", response_model=OrderListResponse, summary="我的订单")
def list_my_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    payment_status: str | None = Query(default=None),
    fulfillment_status: str | None = Query(default=None),
    current_user: dict = Depends(get_current_user),
) -> OrderListResponse:
    result = order_service.list_my_orders(
        user_id=current_user["id"],
        limit=limit,
        offset=offset,
        payment_status=payment_status,
        fulfillment_status=fulfillment_status,
    )
    return OrderListResponse(
        items=[OrderSummary(**mappers.order_summary(o)) for o in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/{order_id}", response_model=OrderDetail, summary="订单详情")
def get_order(order_id: int, current_user: dict = Depends(get_current_user)) -> OrderDetail:
    """订单详情（含明细/支付/物流）。Service 里会校验"这单是不是你的"。"""
    order = order_service.get_order_detail(order_id, user_id=current_user["id"])
    return OrderDetail(**mappers.order_detail(order))


@router.post("/{order_id}/pay", response_model=OrderDetail, summary="模拟支付")
def pay_order(order_id: int, current_user: dict = Depends(get_current_user)) -> OrderDetail:
    """模拟支付：创建支付流水 + 订单转 PAID（事务 + 幂等）。"""
    payment_service.pay_order(order_id=order_id, user_id=current_user["id"])
    order = order_service.get_order_detail(order_id, user_id=current_user["id"])
    return OrderDetail(**mappers.order_detail(order))


@router.post("/{order_id}/cancel", response_model=OrderDetail, summary="取消订单")
def cancel_order(order_id: int, current_user: dict = Depends(get_current_user)) -> OrderDetail:
    """取消订单（仅未支付）+ 回补库存。"""
    order_service.cancel_order(order_id=order_id, user_id=current_user["id"])
    order = order_service.get_order_detail(order_id, user_id=current_user["id"])
    return OrderDetail(**mappers.order_detail(order))


@router.post("/{order_id}/confirm", response_model=OrderDetail, summary="确认收货")
def confirm_order(order_id: int, current_user: dict = Depends(get_current_user)) -> OrderDetail:
    order_service.confirm_receipt(order_id=order_id, user_id=current_user["id"])
    order = order_service.get_order_detail(order_id, user_id=current_user["id"])
    return OrderDetail(**mappers.order_detail(order))


@router.get("/{order_id}/logistics", response_model=LogisticsInfo, summary="查看物流")
def get_logistics(
    order_id: int, current_user: dict = Depends(get_current_user)
) -> LogisticsInfo:
    record = logistics_service.get_logistics(order_id, user_id=current_user["id"])
    return LogisticsInfo(**mappers.logistics_info(record))
