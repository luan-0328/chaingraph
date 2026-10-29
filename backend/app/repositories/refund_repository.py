"""退款流水数据访问。

本层只拼 SQL / flush，**绝不 commit**（事务边界在 Service）。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.refund import ACTIVE_REFUND_STATUSES, RefundRecord, RefundStatus


def create(
    db: Session,
    *,
    refund_no: str,
    after_sale_id: int,
    order_id: int,
    user_id: int,
    amount: Decimal | float,
    reason: str | None = None,
    status: str = RefundStatus.PENDING,
    approved_by: int | None = None,
) -> dict:
    """新建一笔退款流水。

    默认 PENDING：紧接着调用方会调 `claim_success()` 把它条件更新成 SUCCESS。
    这个"先建 PENDING、再条件置成功"的两步不是多余的 ——
    它让并发下只有一个请求能真正把状态推进（见 claim_success 的说明）。

    `after_sale_id` 与 `order_id` 都必填：退款必须挂在某张售后单和某个订单上，
    否则这笔钱没有业务出处，对账时说不清。
    """
    record = RefundRecord(
        refund_no=refund_no,
        after_sale_id=after_sale_id,
        order_id=order_id,
        user_id=user_id,
        amount=Decimal(str(amount)),
        reason=reason,
        status=status,
        approved_by=approved_by,
    )
    db.add(record)
    db.flush()
    db.refresh(record)
    return record.to_dict()


def get_by_id(db: Session, refund_id: int) -> dict | None:
    """按主键取退款流水（字典形式）。"""
    record = db.get(RefundRecord, refund_id)
    return record.to_dict() if record else None


def get_model(db: Session, refund_id: int) -> RefundRecord | None:
    """按主键取退款流水的 **ORM 对象**。"""
    return db.get(RefundRecord, refund_id)


def get_by_no(db: Session, refund_no: str) -> dict | None:
    """按退款单号查（对外暴露的是 refund_no，排查问题时常用它反查）。"""
    record = db.scalar(select(RefundRecord).where(RefundRecord.refund_no == refund_no))
    return record.to_dict() if record else None


def find_pending_by_order(db: Session, order_id: int) -> dict | None:
    """幂等保护：一个订单同一时刻只允许一笔进行中的退款。"""
    record = db.scalar(
        select(RefundRecord)
        .where(RefundRecord.order_id == order_id, RefundRecord.status == RefundStatus.PENDING)
        .order_by(RefundRecord.id.desc())
    )
    return record.to_dict() if record else None


def find_success_by_order(db: Session, order_id: int) -> dict | None:
    """已经退成功的退款单。

    为什么除了 find_pending 还要查 SUCCESS？
        "进行中"只能防住"还没退完就重复提交"；而本项目的退款是**同步即时到账**
        （_execute 里 PENDING → SUCCESS 在同一个事务完成），
        两个并发请求里后到的那个看到的已经不是 PENDING 而是 SUCCESS ——
        只看 PENDING 会漏判，于是同一笔订单被退两次。
    """
    record = db.scalar(
        select(RefundRecord)
        .where(RefundRecord.order_id == order_id, RefundRecord.status == RefundStatus.SUCCESS)
        .order_by(RefundRecord.id.desc())
    )
    return record.to_dict() if record else None


def list_by_order(db: Session, order_id: int) -> list[dict]:
    """某订单的全部退款流水（倒序）—— **含 FAILED 的失败尝试**。

    为什么失败记录也要返回：它是"这笔订单被尝试退过几次"的证据，
    排查"钱怎么退不掉"时很关键。判断"能不能退"请用 find_active_by_order。
    """
    rows = db.scalars(
        select(RefundRecord)
        .where(RefundRecord.order_id == order_id)
        .order_by(RefundRecord.id.desc())
    )
    return [row.to_dict() for row in rows]


def list_by_after_sale(db: Session, after_sale_id: int) -> list[dict]:
    """某售后单下的全部退款流水（倒序）。

    一次售后可能有多笔流水：失败重试是正常业务，所以这里返回的是列表而不是单条。
    """
    rows = db.scalars(
        select(RefundRecord)
        .where(RefundRecord.after_sale_id == after_sale_id)
        .order_by(RefundRecord.id.desc())
    )
    return [row.to_dict() for row in rows]


def list_all(
    db: Session, *, limit: int = 20, offset: int = 0, status: str | None = None,
    user_id: int | None = None,
) -> tuple[list[dict], int]:
    """客服/管理端退款流水列表（可筛状态），返回 (当页数据, 总数)。

    返回 total 是为了让前端能正确分页 —— 只给当页数据的话页码没法算。
    """
    conditions = []
    if user_id is not None:
        conditions.append(RefundRecord.user_id == user_id)
    if status:
        conditions.append(RefundRecord.status == status)

    count_stmt = select(func.count()).select_from(RefundRecord)
    list_stmt = select(RefundRecord)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(RefundRecord.id.desc()).limit(limit).offset(offset))
    return [row.to_dict() for row in rows], total


def claim_success(db: Session, refund_id: int, completed_at: datetime) -> dict | None:
    """**条件更新**：把这一笔退款从 PENDING 抢成 SUCCESS，抢到才返回它。

        UPDATE refund_records SET status='SUCCESS', completed_at=?
         WHERE id=? AND status='PENDING'

    为什么必须带 `AND status='PENDING'`（这是"避免重复退款"的关键一步）：
        拿到行锁之后，代码还要接着改订单 / 售后单 / 回补库存。
        如果只靠"进屋前查过一次状态"，两个并发请求可能都通过了检查；
        而这一条 UPDATE 是数据库在单条语句里完成的"复核 + 落定"，
        affected_rows=0 的那个请求**必须立刻停手**，不许再动任何业务数据。

    这一层之上还有 side_effect_claims 的幂等键和 active_order_id 唯一索引，
    三层里任何一层单独生效都不会重复退钱（见 models/refund.py 的说明）。
    """
    result = db.execute(
        update(RefundRecord)
        .where(
            RefundRecord.id == refund_id,
            RefundRecord.status == RefundStatus.PENDING,
        )
        .values(status=RefundStatus.SUCCESS, completed_at=completed_at)
    )
    if result.rowcount != 1:
        db.flush()
        return None
    db.flush()
    record = db.get(RefundRecord, refund_id)
    return record.to_dict() if record is not None else None


def find_active_by_order(db: Session, order_id: int) -> dict | None:
    """这笔订单当前有没有"占住钱"的退款（PENDING/SUCCESS）。

    PENDING 和 SUCCESS 都要算：前者是"正在退"，后者是"已经退过"。
    """
    record = db.scalar(
        select(RefundRecord)
        .where(
            RefundRecord.order_id == order_id,
            RefundRecord.status.in_(ACTIVE_REFUND_STATUSES),
        )
        .order_by(RefundRecord.id.desc())
    )
    return record.to_dict() if record else None


def count_active_by_order(db: Session, order_id: int) -> int:
    """有效退款笔数（唯一索引的"读"侧对应物，供测试/巡检断言用）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(RefundRecord)
            .where(
                RefundRecord.order_id == order_id,
                RefundRecord.status.in_(ACTIVE_REFUND_STATUSES),
            )
        )
        or 0
    )


def mark_failed(db: Session, refund_id: int, *, approved_by: int | None = None) -> dict | None:
    """把一笔退款标成 FAILED（渠道退款失败、或人工拒绝）。

    标成 FAILED 之后 `active_order_id` 生成列会变成 NULL，
    于是这个订单**重新变成"可以再退"** —— 这正是"失败不占位"的实现方式。
    所以这个方法只用于"确实失败了"，不能拿它当"取消"用。
    """
    record = db.get(RefundRecord, refund_id)
    if record is None:
        return None
    record.status = RefundStatus.FAILED
    if approved_by is not None:
        record.approved_by = approved_by
    db.flush()
    db.refresh(record)
    return record.to_dict()
