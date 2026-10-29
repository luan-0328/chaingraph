"""购物车接口（v2.md §45 Cart）。"""

from fastapi import APIRouter, Depends, status

from app.api import mappers
from app.core.dependencies import get_current_user
from app.core.logging import get_logger
from app.schemas.cart import CartItemAdd, CartItemInfo, CartItemUpdate, CartResponse
from app.services import cart_service

logger = get_logger(__name__)

router = APIRouter(prefix="/cart", tags=["cart"])


@router.get("", response_model=CartResponse, summary="查看购物车")
def get_cart(current_user: dict = Depends(get_current_user)) -> CartResponse:
    result = cart_service.get_cart(current_user["id"])
    return CartResponse(
        items=[CartItemInfo(**mappers.cart_item_info(item)) for item in result["items"]],
        total_quantity=result["total_quantity"],
        total_amount=result["total_amount"],
    )


@router.post(
    "/items",
    response_model=CartItemInfo,
    status_code=status.HTTP_201_CREATED,
    summary="加入购物车",
)
def add_item(
    payload: CartItemAdd,
    current_user: dict = Depends(get_current_user),
) -> CartItemInfo:
    item = cart_service.add_to_cart(
        user_id=current_user["id"],
        product_id=payload.product_id,
        quantity=payload.quantity,
    )
    return CartItemInfo(**mappers.cart_item_info(item))


@router.patch("/items/{item_id}", response_model=CartItemInfo, summary="修改数量")
def update_item(
    item_id: int,
    payload: CartItemUpdate,
    current_user: dict = Depends(get_current_user),
) -> CartItemInfo:
    item = cart_service.update_quantity(
        user_id=current_user["id"], item_id=item_id, quantity=payload.quantity
    )
    return CartItemInfo(**mappers.cart_item_info(item))


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除购物车项")
def remove_item(item_id: int, current_user: dict = Depends(get_current_user)) -> None:
    cart_service.remove_item(user_id=current_user["id"], item_id=item_id)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, summary="清空购物车")
def clear_cart(current_user: dict = Depends(get_current_user)) -> None:
    cart_service.clear_cart(current_user["id"])
