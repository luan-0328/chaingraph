"""售后单数据访问。"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.after_sale import AfterSale, AfterSaleStatus


def create(
    db: Session,
    *,
    after_sale_no: str,
    order_id: int,
    user_id: int,
    type: str,
    amount: Decimal | float,
    reason: str | None = None,
    status: str = AfterSaleStatus.APPLYING,
) -> dict:
    """新建售后单（默认 APPLYING）。

    售后单是**业务对象**，工单（Ticket）是它的处理载体（v2.md §14）：
    用户可能只提了工单没提售后，那种情况由 Service 补建一张售后单，
    保证"退款一定有售后单可挂"。
    """
    record = AfterSale(
        after_sale_no=after_sale_no,
        order_id=order_id,
        user_id=user_id,
        type=type,
        amount=Decimal(str(amount)),
        reason=reason,
        status=status,
    )
    db.add(record)
    db.flush()
    db.refresh(record)
    return record.to_dict()


def get_by_id(db: Session, after_sale_id: int) -> dict | None:
    """按主键取售后单（字典形式）。"""
    record = db.get(AfterSale, after_sale_id)
    return record.to_dict() if record else None


def get_model(db: Session, after_sale_id: int) -> AfterSale | None:
    """取售后单的 **ORM 对象**（要改状态/追加拒绝原因时用）。"""
    return db.get(AfterSale, after_sale_id)


def find_active_by_order(db: Session, order_id: int) -> dict | None:
    """该订单是否已有进行中的售后（幂等：防止重复申请）。"""
    record = db.scalar(
        select(AfterSale)
        .where(
            AfterSale.order_id == order_id,
            AfterSale.status.in_(
                (
                    AfterSaleStatus.APPLYING,
                    AfterSaleStatus.PROCESSING,
                    AfterSaleStatus.APPROVED,
                )
            ),
        )
        .order_by(AfterSale.id.desc())
    )
    return record.to_dict() if record else None


def list_by_user(
    db: Session, *, user_id: int, limit: int = 20, offset: int = 0
) -> tuple[list[dict], int]:
    """某用户的售后单列表。

    `user_id` 是必填且无条件进 WHERE —— "我的售后"不会泄露别人的申请。
    """
    conditions = [AfterSale.user_id == user_id]
    return _list(db, conditions, limit, offset)


def list_all(
    db: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    order_id: int | None = None,
) -> tuple[list[dict], int]:
    """客服/管理端售后列表（可按状态、订单筛）。"""
    conditions = []
    if status:
        conditions.append(AfterSale.status == status)
    if order_id:
        conditions.append(AfterSale.order_id == order_id)
    return _list(db, conditions, limit, offset)


def _list(db: Session, conditions: list, limit: int, offset: int) -> tuple[list[dict], int]:
    """分页查询的公共实现（列表 + 总数）。

    抽出来是因为"自己的"和"全部的"只有 WHERE 不同，
    复制两份迟早会出现"一边加了过滤、另一边忘了"的不一致。
    """
    count_stmt = select(func.count()).select_from(AfterSale)
    list_stmt = select(AfterSale)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(AfterSale.id.desc()).limit(limit).offset(offset))
    return [row.to_dict() for row in rows], total


def update_status(db: Session, after_sale_id: int, status: str, *, completed_at=None) -> dict | None:
    """改售后单状态，可选带上完成时间。

    **无条件更新**（不是条件更新）：售后单状态是"申请进度"的镜像，
    不直接对应钱 —— 钱的安全性由 refund_records 那边的条件更新负责。
    这个区别是刻意的：如果这里也做条件更新，退款流程要在两张表上
    各维护一套状态前置条件，反而更容易写错。
    """
    record = db.get(AfterSale, after_sale_id)
    if record is None:
        return None

    record.status = status
    if completed_at is not None:
        record.completed_at = completed_at

    db.flush()
    db.refresh(record)
    return record.to_dict()
