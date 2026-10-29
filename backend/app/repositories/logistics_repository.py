"""物流数据访问。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.logistics import Logistics, LogisticsStatus


def create(
    db: Session,
    *,
    order_id: int,
    tracking_no: str,
    company: str,
    status: str = LogisticsStatus.PENDING,
    latest_message: str | None = None,
    last_update_time: datetime | None = None,
) -> dict:
    """建物流单（客服点"发货"时调用，与订单改 SHIPPED 在同一个事务里）。

    `last_update_time` 要显式传：Agent 的 `query_logistics` 工具会算
    "多久没更新了"来判断物流异常，没有这个时间就没法算停滞时长。
    """
    record = Logistics(
        order_id=order_id,
        tracking_no=tracking_no,
        company=company,
        status=status,
        latest_message=latest_message,
        last_update_time=last_update_time,
    )
    db.add(record)
    db.flush()
    db.refresh(record)
    return record.to_dict()


def get_by_order_id(db: Session, order_id: int) -> dict | None:
    """按订单查物流（取最新一条），不存在返回 None。

    返回 None 表示"还没发货" —— 这是**正常业务状态**，不是错误，
    调用方（尤其是 Agent 工具）应当如实告诉用户"未发货"，而不是抛异常。
    """
    record = db.scalar(
        select(Logistics).where(Logistics.order_id == order_id).order_by(Logistics.id.desc())
    )
    return record.to_dict() if record else None


def get_model_by_order(db: Session, order_id: int) -> Logistics | None:
    """取物流单的 **ORM 对象**（改状态用），同样取最新一条。"""
    return db.scalar(
        select(Logistics).where(Logistics.order_id == order_id).order_by(Logistics.id.desc())
    )


def update_status(
    db: Session,
    order_id: int,
    *,
    status: str,
    latest_message: str | None = None,
    last_update_time: datetime | None = None,
) -> dict | None:
    """推进物流状态（发货 → 运输中 → 已签收），返回 None 表示该订单还没有物流单。

    `latest_message` 是给用户看的一句话（"包裹已到达杭州转运中心"），
    每次推进都该更新 —— 用户在前端看到的就是这句话。
    """
    record = get_model_by_order(db, order_id)
    if record is None:
        return None

    record.status = status
    if latest_message is not None:
        record.latest_message = latest_message
    if last_update_time is not None:
        record.last_update_time = last_update_time

    db.flush()
    db.refresh(record)
    return record.to_dict()
