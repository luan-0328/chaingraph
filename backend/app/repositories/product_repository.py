"""商品数据访问。"""

from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.product import Product, ProductStatus


def create(
    db: Session,
    *,
    name: str,
    price: Decimal | float,
    stock: int = 0,
    description: str | None = None,
    status: str = ProductStatus.ON_SALE,
) -> dict:
    """新建商品（管理员接口用）。默认 ON_SALE。

    price 走 `Decimal(str(...))`：钱字段不接受二进制浮点的误差。
    """
    product = Product(
        name=name,
        price=Decimal(str(price)),
        stock=stock,
        description=description,
        status=status,
    )
    db.add(product)
    db.flush()
    db.refresh(product)
    return product.to_dict()


def get_by_id(db: Session, product_id: int) -> dict | None:
    """按主键取商品（字典形式）。"""
    product = db.get(Product, product_id)
    return product.to_dict() if product else None


def get_model(db: Session, product_id: int) -> Product | None:
    """需要改动实体（扣库存/改状态）时用这个拿 ORM 对象。"""
    return db.get(Product, product_id)


def list_products(
    db: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    keyword: str | None = None,
    status: str | None = None,
    only_on_sale: bool = False,
) -> tuple[list[dict], int]:
    """商品列表（用户端浏览 + 管理端维护共用），返回 (当页数据, 总数)。

    `only_on_sale` 与 `status` 是互斥语义，故意写成 if/elif：
        用户端传 only_on_sale=True → 只看到在售的（下架商品不该出现在货架上）
        管理端传 status="OFF_SALE"  → 专门看下架的
    如果两个都传，以 only_on_sale 为准 —— 用户端的安全性优先于管理端的灵活性。

    keyword 同时搜名称和描述：用户搜"耳机"时，描述里写"蓝牙耳机"的也该命中。
    """
    conditions = []
    if keyword:
        like = f"%{keyword}%"
        conditions.append(or_(Product.name.like(like), Product.description.like(like)))
    if only_on_sale:
        conditions.append(Product.status == ProductStatus.ON_SALE)
    elif status:
        conditions.append(Product.status == status)

    count_stmt = select(func.count()).select_from(Product)
    list_stmt = select(Product)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(Product.id.desc()).limit(limit).offset(offset))
    return [product.to_dict() for product in rows], total


def update(
    db: Session,
    product_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    price: Decimal | float | None = None,
    stock: int | None = None,
    status: str | None = None,
) -> dict | None:
    """部分更新商品（None = 不改该字段），返回 None 表示商品不存在。

    **stock 走直接赋值而不是条件更新** —— 这是管理员的手工修正入口，
    不是下单扣减路径。下单必须走下面的 `decrease_stock`（带 `WHERE stock >= ?`）。
    谁要是拿这个方法去扣库存，就绕过了超卖保护。
    """
    product = db.get(Product, product_id)
    if product is None:
        return None

    if name is not None:
        product.name = name
    if description is not None:
        product.description = description
    if price is not None:
        product.price = Decimal(str(price))
    if stock is not None:
        product.stock = stock
    if status is not None:
        product.status = status

    db.flush()
    db.refresh(product)
    return product.to_dict()


def decrease_stock(db: Session, product_id: int, quantity: int) -> bool:
    """扣减库存。返回是否成功（库存不足返回 False，由 Service 抛业务异常）。

    用**单条条件 UPDATE** 完成"判断 + 扣减"：

        UPDATE products SET stock = stock - :q WHERE id = :id AND stock >= :q

    affected_rows = 1 才算扣成功；0 表示库存不够（或商品不存在）。
    为什么不用 `SELECT ... FOR UPDATE` 再判断：那样虽然也正确，但多一次往返、
    并且要先把行锁住再放弃；条件更新是原子的，数据库层就把"不能超卖"这件事做完了。
    并发 10 个请求抢 3 件库存时，只有 3 个能拿到 affected_rows = 1。

    注意：MySQL 的 UPDATE 拿的是排他行锁，"同一行的并发修改"是串行的，
    所以这里不需要额外加锁；真正的保障是 WHERE 里的 stock >= :q。
    """
    from sqlalchemy import update as sql_update

    if quantity <= 0:
        raise ValueError("扣减数量必须大于 0")

    result = db.execute(
        sql_update(Product)
        .where(Product.id == product_id, Product.stock >= quantity)
        .values(stock=Product.stock - quantity)
    )
    db.flush()
    # SQLAlchemy 默认 synchronize_session="auto"：匹配条件的对象会同步成新值，
    # 所以同一个 session 里后续读到的是扣减后的库存（不需要 expire_all 再多查一次）
    return (result.rowcount or 0) > 0


def increase_stock(db: Session, product_id: int, quantity: int) -> None:
    """回补库存（取消订单/未发货退款时用）：同样是单条原子 UPDATE。"""
    from sqlalchemy import update as sql_update

    if quantity <= 0:
        return
    db.execute(
        sql_update(Product)
        .where(Product.id == product_id)
        .values(stock=Product.stock + quantity)
    )
    db.flush()
