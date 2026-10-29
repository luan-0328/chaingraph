"""购物车数据访问。"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.cart import CartItem


def add_item(db: Session, *, user_id: int, product_id: int, quantity: int) -> dict:
    """加购：同一商品累加数量（唯一键 uk_cart_user_product 保证只有一行）。"""
    item = db.scalar(
        select(CartItem).where(CartItem.user_id == user_id, CartItem.product_id == product_id)
    )
    if item is None:
        item = CartItem(user_id=user_id, product_id=product_id, quantity=quantity)
        db.add(item)
    else:
        item.quantity += quantity

    db.flush()
    db.refresh(item)
    return item.to_dict()


def get_by_id(db: Session, item_id: int) -> dict | None:
    """按主键取购物车行（字典形式）。

    **注意它不校验归属** —— 归属校验在 Service（cart_service）做，
    因为仓储拿不到"当前登录用户"这个概念。任何按 item_id 操作的方法
    调用前都必须先确认这一行属于当前用户。
    """
    item = db.get(CartItem, item_id)
    return item.to_dict() if item else None


def list_by_user(db: Session, user_id: int) -> list[dict]:
    """某用户的购物车（按加入顺序，不排序 —— 用户自己加的顺序就是他要的顺序）。"""
    rows = db.scalars(
        select(CartItem).where(CartItem.user_id == user_id).order_by(CartItem.id.asc())
    )
    return [item.to_dict() for item in rows]


def list_models_by_ids(db: Session, user_id: int, item_ids: list[int]) -> list[CartItem]:
    """结算时按 id 取购物车行（并校验归属）。"""
    if not item_ids:
        return []
    rows = db.scalars(
        select(CartItem).where(CartItem.user_id == user_id, CartItem.id.in_(item_ids))
    )
    return list(rows)


def update_quantity(db: Session, item_id: int, quantity: int) -> dict | None:
    """改购物车行数量（调用方负责先校验归属与商品可购）。"""
    item = db.get(CartItem, item_id)
    if item is None:
        return None
    item.quantity = quantity
    db.flush()
    db.refresh(item)
    return item.to_dict()


def remove_item(db: Session, item_id: int) -> bool:
    """删一行购物车，返回是否删到了（False = 已经不存在）。"""
    item = db.get(CartItem, item_id)
    if item is None:
        return False
    db.delete(item)
    db.flush()
    return True


def remove_items(db: Session, item_ids: list[int]) -> int:
    """下单成功后清掉已结算的购物车项。"""
    if not item_ids:
        return 0
    result = db.execute(delete(CartItem).where(CartItem.id.in_(item_ids)))
    db.flush()
    return int(result.rowcount or 0)
