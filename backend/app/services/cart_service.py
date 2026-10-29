"""购物车服务。

职责：
    - 加购 / 改数量 / 删除 / 查看；
    - 查看时关联商品信息（名称、当前价格、库存、状态）并算出小计与合计；
    - 校验当前用户才能操作自己的购物车行（v2.md §59 数据归属）。
"""

from app.core.database import session_scope
from app.core.exceptions import (
    CartItemNotFoundError,
    PermissionDeniedError,
)
from app.core.logging import get_logger
from app.repositories import cart_repository, product_repository, user_repository
from app.services import product_service

logger = get_logger(__name__)


def add_to_cart(*, user_id: int, product_id: int, quantity: int) -> dict:
    """加购：先校验商品可购，再累加数量（同一商品累加，不新增行）。"""
    with session_scope() as db:
        user_repository.lock_cart_owner(db, user_id)
        product = product_service.ensure_purchasable(db, product_id, quantity)
        item = cart_repository.add_item(
            db, user_id=user_id, product_id=product_id, quantity=quantity
        )

    logger.info("加入购物车：user=%s product=%s x%s", user_id, product_id, quantity)
    return _enrich(item, product)


def get_cart(user_id: int) -> dict:
    """购物车详情：把商品当前信息拼进来（名称/当前价格/库存/状态）。"""
    with session_scope() as db:
        items = cart_repository.list_by_user(db, user_id)
        # 一次把涉及的商品都取出来，避免 N+1
        products = {
            item["product_id"]: product_repository.get_by_id(db, item["product_id"])
            for item in items
        }

    enriched = [_enrich(item, products.get(item["product_id"])) for item in items]
    return {
        "items": enriched,
        "total_quantity": sum(item["quantity"] for item in enriched),
        "total_amount": round(sum(item["subtotal"] or 0 for item in enriched), 2),
    }


def update_quantity(*, user_id: int, item_id: int, quantity: int) -> dict:
    """改购物车行数量。

    每一次改数量都重新跑一遍 `ensure_purchasable`：商品可能在这期间下架了、
    或者库存被卖光了。只在"加购时校验一次"是不够的 ——
    购物车可以放很久，那个校验结果早就过期了。
    """
    with session_scope() as db:
        user_repository.lock_cart_owner(db, user_id)
        item = _get_owned_item(db, user_id, item_id)
        product = product_service.ensure_purchasable(db, item["product_id"], quantity)
        updated = cart_repository.update_quantity(db, item_id, quantity)

    return _enrich(updated, product)


def remove_item(*, user_id: int, item_id: int) -> None:
    """删除一行购物车（先 `_get_owned_item` 校验归属，别人的行删不掉）。"""
    with session_scope() as db:
        user_repository.lock_cart_owner(db, user_id)
        _get_owned_item(db, user_id, item_id)
        cart_repository.remove_item(db, item_id)

    logger.info("移除购物车项：user=%s item=%s", user_id, item_id)


def clear_cart(user_id: int) -> int:
    """清空购物车，返回删掉几行。

    先 list 再按 id 批量删（而不是 `DELETE WHERE user_id=?` 一把梭）：
    返回值要告诉前端"清掉了几件"，而且批量删复用了 remove_items 这条路径。
    """
    with session_scope() as db:
        user_repository.lock_cart_owner(db, user_id)
        items = cart_repository.list_by_user(db, user_id)
        return cart_repository.remove_items(db, [item["id"] for item in items])


# ======================================================================
# 内部工具
# ======================================================================
def _get_owned_item(db, user_id: int, item_id: int) -> dict:
    """取购物车行并校验归属 —— 越权直接 403。"""
    item = cart_repository.get_by_id(db, item_id)
    if item is None:
        raise CartItemNotFoundError(f"购物车项 {item_id} 不存在")
    if item["user_id"] != user_id:
        raise PermissionDeniedError("无权操作他人的购物车")
    return item


def _enrich(item: dict, product: dict | None) -> dict:
    """购物车行 + 商品信息 → 接口返回结构。

    product 为 None 表示商品已被删除，价格/小计置空（前端显示"商品已失效"）。
    """
    price = product["price"] if product else None
    quantity = item["quantity"]
    return {
        "item_id": item["id"],
        "product_id": item["product_id"],
        "product_name": product["name"] if product else None,
        "price": price,
        "stock": product["stock"] if product else None,
        "product_status": product["status"] if product else None,
        "quantity": quantity,
        "subtotal": round(price * quantity, 2) if price is not None else None,
        "added_at": item["created_at"],
    }
