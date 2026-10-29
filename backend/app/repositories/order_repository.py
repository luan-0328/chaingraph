"""订单 + 订单明细数据访问。"""

from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.order import Order, OrderAfterSaleStatus, PaymentStatus
from app.models.order_item import OrderItem


# ======================================================================
# 订单
# ======================================================================
def create(
    db: Session,
    *,
    order_no: str,
    user_id: int,
    total_amount: Decimal | float,
) -> dict:
    """新建订单（只写订单头，明细由 add_items 单独写）。

    必须在**下单那个事务**里调用：订单头、明细、扣库存、清购物车要一起提交，
    否则会出现"订单建了但库存没扣"或"库存扣了但订单没建"。
    本函数只 flush 不 commit —— 事务边界在 Service（core/database.session_scope）。

    三个"状态维度"都留默认初值（UNPAID / PENDING / NONE），由后续业务动作推进，
    所以这里不接收状态参数：下单时状态唯一，没有第二种可能。
    """
    order = Order(order_no=order_no, user_id=user_id, total_amount=Decimal(str(total_amount)))
    db.add(order)
    db.flush()
    db.refresh(order)
    return order.to_dict()


def get_by_id(db: Session, order_id: int) -> dict | None:
    """按主键取订单（字典形式，只读）。"""
    order = db.get(Order, order_id)
    return order.to_dict() if order else None


def get_model(db: Session, order_id: int) -> Order | None:
    """需要改状态的场景用它拿 ORM 对象（配合行锁见 get_model_for_update）。"""
    return db.get(Order, order_id)


def get_model_for_update(db: Session, order_id: int) -> Order | None:
    """加行锁读订单 —— 支付/退款这类并发敏感操作必须用它。"""
    return db.scalar(select(Order).where(Order.id == order_id).with_for_update())


def get_by_no(db: Session, order_no: str) -> dict | None:
    """按订单号查（对外暴露的是 order_no，客服/用户口述的也是它）。"""
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    return order.to_dict() if order else None


def list_by_user(
    db: Session,
    *,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
    payment_status: str | None = None,
    fulfillment_status: str | None = None,
) -> tuple[list[dict], int]:
    """某用户的订单列表（可再按支付/履约状态筛），返回 (当页数据, 总数)。

    `user_id` 是**必填**且无条件加进 WHERE ——
    这是"我的订单"接口不会泄露别人订单的根本保证：
    它不是一个可选的筛选项，调用方没有"忘记传"的机会。
    """
    conditions = [Order.user_id == user_id]
    if payment_status:
        conditions.append(Order.payment_status == payment_status)
    if fulfillment_status:
        conditions.append(Order.fulfillment_status == fulfillment_status)

    count_stmt = select(func.count()).select_from(Order)
    list_stmt = select(Order)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(Order.id.desc()).limit(limit).offset(offset))
    return [order.to_dict() for order in rows], total


def list_all(
    db: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    keyword: str | None = None,
    payment_status: str | None = None,
    fulfillment_status: str | None = None,
    user_id: int | None = None,
) -> tuple[list[dict], int]:
    """客服/管理端订单列表。"""
    conditions = []
    if user_id is not None:
        conditions.append(Order.user_id == user_id)
    if payment_status:
        conditions.append(Order.payment_status == payment_status)
    if fulfillment_status:
        conditions.append(Order.fulfillment_status == fulfillment_status)
    if keyword:
        like = f"%{keyword}%"
        conditions.append(or_(Order.order_no.like(like)))

    count_stmt = select(func.count()).select_from(Order)
    list_stmt = select(Order)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(Order.id.desc()).limit(limit).offset(offset))
    return [order.to_dict() for order in rows], total


def set_after_sale_status(db: Session, order, status: str) -> dict:
    """更新订单的**售后维度**状态（NONE / APPLYING / REFUNDING / REFUNDED）。

    参数是 ORM 对象而不是 id：调用方通常已经拿着加锁后的对象在同一个事务里改多处，
    再查一次反而多一个"两次读到的不是同一行"的机会。

    注意它是**无条件**改字段 —— 售后状态是"申请进度"的镜像，不像支付/退款那样
    直接对应钱，所以这里保留简单写法；钱的维度一律走下面的条件更新。
    """
    order.after_sale_status = status
    db.flush()
    db.refresh(order)
    return order.to_dict()


# 注意：这里**没有** set_paid / mark_refunded 这类"无条件改状态"的方法。
#
# 它们曾经存在，而且正是重复支付 / 重复退款的隐患所在：
# 拿到 ORM 对象直接改字段，等于把"当前状态是否允许这一步"完全交给调用方记忆。
# 只要有一个调用方忘了先查状态，并发下就会改出脏状态（钱退两次、订单被重复标记）。
#
# 现在支付与退款只提供两个**条件更新**入口，检查与修改在同一条 SQL 里完成：
#     claim_paid()       UPDATE ... WHERE payment_status='UNPAID'
#     claim_refunded()   UPDATE ... WHERE payment_status='PAID'
# 调用方必须拿 affected_rows 当判据（见 services/payment_service、refund_service）。


def claim_paid(db: Session, order_id: int, paid_at) -> bool:
    """**条件更新**：`UPDATE orders SET payment_status='PAID' WHERE id=? AND payment_status='UNPAID'`。

    返回是否真的改到了那一行。返回 False = 这个订单已经被别的请求支付过
    （或已被取消/退款），调用方必须停手并回滚。
    """
    return _conditional_status_update(
        db,
        order_id,
        expected=PaymentStatus.UNPAID,
        values={"payment_status": PaymentStatus.PAID, "paid_at": paid_at},
    )


def claim_refunded(db: Session, order_id: int, *, refunded_at) -> bool:
    """**条件更新**：`UPDATE orders SET payment_status='REFUNDED' WHERE id=? AND payment_status='PAID'`。

    这是"避免重复退款"里真正落地的那一步：只有"当前还是 PAID"的订单能被改成
    REFUNDED，改到了才说明本次退款有效。返回 False 的请求必须抛错回滚，
    连带它刚抢到的退款流水一起撤销 —— 绝不留下"订单没退但流水已成功"的脏数据。
    """
    return _conditional_status_update(
        db,
        order_id,
        expected=PaymentStatus.PAID,
        values={
            "payment_status": PaymentStatus.REFUNDED,
            "after_sale_status": OrderAfterSaleStatus.REFUNDED,
            "completed_at": refunded_at,
        },
    )


def _conditional_status_update(
    db: Session, order_id: int, *, expected: str, values: dict
) -> bool:
    """带状态前置条件的 UPDATE，返回 affected_rows == 1。

    为什么统一走这里：支付、退款都要"只有处于某个状态时才能改"，
    各写一遍迟早写歪一个。条件写在 WHERE 里，检查与修改由数据库在
    同一条语句内完成，没有"先查后改"的竞态窗口。
    """
    result = db.execute(
        update(Order).where(Order.id == order_id, Order.payment_status == expected).values(**values)
    )
    db.flush()
    return result.rowcount == 1


# ======================================================================
# 订单明细
# ======================================================================
def add_items(db: Session, *, order_id: int, items: list[dict]) -> list[dict]:
    """批量写入订单明细。

    items 里每一项必须已经带好商品快照（product_name / product_price），
    这是 v2.md §9.2 的硬要求。
    """
    rows = [
        OrderItem(
            order_id=order_id,
            product_id=item["product_id"],
            product_name=item["product_name"],
            product_price=Decimal(str(item["product_price"])),
            quantity=item["quantity"],
            subtotal=Decimal(str(item["subtotal"])),
        )
        for item in items
    ]
    db.add_all(rows)
    db.flush()
    return [row.to_dict() for row in rows]


def list_by_order(db: Session, order_id: int) -> list[dict]:
    """某订单的商品明细（正序）。

    每一条都带**下单时的商品快照**（product_name / product_price）——
    所以商品后来改价改名都不影响历史订单（v2.md §9.2 的硬要求）。
    退款回补库存时也是遍历它（按下单数量一件件还回去）。
    """
    rows = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id.asc())
    )
    return [row.to_dict() for row in rows]
