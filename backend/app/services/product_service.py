"""商品服务。"""

from decimal import Decimal

from app.core.database import session_scope
from app.core.exceptions import (
    InsufficientStockError,
    ProductNotFoundError,
    ProductOffSaleError,
)
from app.core.logging import get_logger
from app.models.product import ProductStatus
from app.repositories import product_repository

logger = get_logger(__name__)


def list_products(
    *,
    limit: int = 20,
    offset: int = 0,
    keyword: str | None = None,
    status: str | None = None,
    only_on_sale: bool = False,
) -> dict:
    """商品列表。普通用户只看上架商品（only_on_sale=True），管理端可看全部。"""
    with session_scope() as db:
        items, total = product_repository.list_products(
            db,
            limit=limit,
            offset=offset,
            keyword=keyword,
            status=status,
            only_on_sale=only_on_sale,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_product(product_id: int, *, only_on_sale: bool = False) -> dict:
    """商品详情。

    `only_on_sale=True` 是**用户端**入口：下架商品要当成"不存在"来对待
    （抛 ProductOffSaleError 而不是静默返回），否则用户能靠改 URL 看到
    已下架商品的详情页并尝试加购。
    管理端传 False —— 他们本来就要看下架商品才能重新上架。
    """
    with session_scope() as db:
        product = product_repository.get_by_id(db, product_id)

    if product is None:
        raise ProductNotFoundError(f"商品 {product_id} 不存在")
    if only_on_sale and product["status"] != ProductStatus.ON_SALE:
        raise ProductOffSaleError(f"商品「{product['name']}」已下架")
    return product


def create_product(
    *,
    name: str,
    price: Decimal | float,
    stock: int,
    description: str | None = None,
    status: str = ProductStatus.ON_SALE,
) -> dict:
    """新建商品（仅管理端）。默认直接上架 —— 管理员建商品通常就是要卖它。"""
    with session_scope() as db:
        product = product_repository.create(
            db, name=name, price=price, stock=stock, description=description, status=status
        )
    logger.info("创建商品：id=%s name=%s price=%s", product["id"], name, price)
    return product


def update_product(product_id: int, **fields) -> dict:
    """改商品（只透传仓储支持的字段）。

    注意 `stock` 也在这里可改 —— 它是**管理员手工修正库存**的入口。
    下单扣减绝不该走这条路（那会绕过 `WHERE stock >= ?` 的超卖保护）。
    """
    with session_scope() as db:
        product = product_repository.update(db, product_id, **fields)
    if product is None:
        raise ProductNotFoundError(f"商品 {product_id} 不存在")
    logger.info("更新商品：id=%s 字段=%s", product_id, list(fields))
    return product


def take_off_sale(product_id: int) -> dict:
    """下架（不物理删除：历史订单明细要能追溯到商品）。"""
    return update_product(product_id, status=ProductStatus.OFF_SALE)


def ensure_purchasable(db, product_id: int, quantity: int) -> dict:
    """下单/加购前的可购校验（在调用方的事务里执行）。

    这里不自己开事务：它必须和"扣库存"在同一个事务里，否则会出现
    校验通过但扣减失败、或者校验和扣减之间被别人插队的情况。
    """
    product = product_repository.get_by_id(db, product_id)
    if product is None:
        raise ProductNotFoundError(f"商品 {product_id} 不存在")
    if product["status"] != ProductStatus.ON_SALE:
        raise ProductOffSaleError(f"商品「{product['name']}」已下架，无法购买")
    if product["stock"] < quantity:
        raise InsufficientStockError(
            f"商品「{product['name']}」库存不足（剩余 {product['stock']}，需要 {quantity}）"
        )
    return product
