"""售后接口（v2.md §45 AfterSale）。"""

from fastapi import APIRouter, Depends, Query, status

from app.api import mappers
from app.core.dependencies import get_current_user
from app.core.logging import get_logger
from app.schemas.after_sale import (
    AfterSaleCancelRequest,
    AfterSaleCreate,
    AfterSaleInfo,
    AfterSaleListResponse,
    RefundApplyResponse,
)
from app.services import after_sale_service, refund_service

logger = get_logger(__name__)

router = APIRouter(prefix="/after-sales", tags=["after-sales"])


@router.post("", response_model=AfterSaleInfo, status_code=status.HTTP_201_CREATED, summary="申请售后")
def create_after_sale(
    payload: AfterSaleCreate,
    current_user: dict = Depends(get_current_user),
) -> AfterSaleInfo:
    """申请售后（校验订单归属、已支付、无进行中的售后）。"""
    sale = after_sale_service.create_after_sale(
        user_id=current_user["id"],
        order_id=payload.order_id,
        type=payload.type,
        reason=payload.reason,
        amount=payload.amount,
    )
    return AfterSaleInfo(**mappers.after_sale_info(sale))


@router.get("", response_model=AfterSaleListResponse, summary="我的售后")
def list_my_after_sales(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> AfterSaleListResponse:
    result = after_sale_service.list_after_sales(
        user_id=current_user["id"], limit=limit, offset=offset
    )
    return AfterSaleListResponse(
        items=[AfterSaleInfo(**mappers.after_sale_info(s)) for s in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/{after_sale_id}", response_model=AfterSaleInfo, summary="售后详情")
def get_after_sale(
    after_sale_id: int, current_user: dict = Depends(get_current_user)
) -> AfterSaleInfo:
    sale = after_sale_service.get_after_sale(after_sale_id, user_id=current_user["id"])
    return AfterSaleInfo(**mappers.after_sale_info(sale))


@router.post("/{after_sale_id}/cancel", response_model=AfterSaleInfo, summary="撤销申请")
def cancel_after_sale(
    after_sale_id: int,
    payload: AfterSaleCancelRequest | None = None,
    current_user: dict = Depends(get_current_user),
) -> AfterSaleInfo:
    sale = after_sale_service.cancel_after_sale(
        after_sale_id=after_sale_id,
        user_id=current_user["id"],
        reason=payload.reason if payload else None,
    )
    return AfterSaleInfo(**mappers.after_sale_info(sale))


@router.post("/{after_sale_id}/refund", response_model=RefundApplyResponse, summary="申请退款")
def apply_refund(
    after_sale_id: int,
    current_user: dict = Depends(get_current_user),
) -> RefundApplyResponse:
    """对售后单发起退款。

    两种结局（都要如实告诉前端）：
        executed=True           金额/状态都符合规则 → 当场自动退款
        need_human=True         已发货或超自动上限 → 转人工审核（客服工作台审批）
    具体规则见 refund_service（v2.md §17）。
    """
    sale = after_sale_service.get_after_sale(after_sale_id, user_id=current_user["id"])

    result = refund_service.apply_refund(
        order_id=sale["order_id"],
        user_id=current_user["id"],
        amount=sale["amount"],
        reason=sale.get("reason"),
        after_sale_id=after_sale_id,
    )

    updated = after_sale_service.get_after_sale(after_sale_id, user_id=current_user["id"])
    return RefundApplyResponse(
        after_sale=AfterSaleInfo(**mappers.after_sale_info(updated)),
        executed=result["executed"],
        need_human=result["need_human"],
        reason=result["reason"],
        refund=mappers.refund_info(result["refund"]) if result.get("refund") else None,
    )
