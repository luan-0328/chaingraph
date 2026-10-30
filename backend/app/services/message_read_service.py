"""用户与每位客服独立已读；明确提交渲染过的消息 ID，不自动吞掉后到的新消息。"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.mysql import insert

from app.core.database import session_scope
from app.core.exceptions import PermissionDeniedError, ValidationError
from app.models.ticket import Ticket
from app.models.ticket_message import TicketMessage
from app.models.ticket_read import TicketRead
from app.models.user import Role


def authorize(db, ticket_id: int, viewer: dict):
    """统一消息读取权限；客服可查看工单，普通用户只能查看自己的。"""
    from app.services.agent_task_service import lock_ticket_run
    ticket, _ = lock_ticket_run(db, ticket_id)
    if viewer["role"] not in Role.STAFF and ticket.user_id != viewer["id"]:
        raise PermissionDeniedError("无权查看他人的工单消息")
    return ticket


def unread_query(viewer_id: int):
    """排除系统提示和本人消息；未读是收件人的阅读状态，不是发送状态。"""
    return select(TicketMessage.ticket_id, func.count().label("unread_count")).outerjoin(
        TicketRead, and_(TicketRead.ticket_id == TicketMessage.ticket_id, TicketRead.user_id == viewer_id),
    ).where(TicketMessage.id > func.coalesce(TicketRead.last_message_id, 0),
            TicketMessage.sender_type != "SYSTEM",
            or_(TicketMessage.sender_id.is_(None), TicketMessage.sender_id != viewer_id)
    ).group_by(TicketMessage.ticket_id)


def annotate(db, tickets: list[dict], viewer_id: int | None) -> list[dict]:
    """一条分组查询补齐一页工单的未读数量，避免逐条查消息。"""
    if not tickets or viewer_id is None:
        return tickets
    counts = dict(db.execute(unread_query(viewer_id).where(TicketMessage.ticket_id.in_([t["id"] for t in tickets]))).all())
    return [{**ticket, "unread_count": int(counts.get(ticket["id"], 0))} for ticket in tickets]


def summary(viewer: dict) -> dict:
    """只统计该账号有权访问的工单，轮询此接口不会修改已读状态。"""
    with session_scope() as db:
        statement = unread_query(viewer["id"]).join(Ticket, Ticket.id == TicketMessage.ticket_id)
        if viewer["role"] not in Role.STAFF:
            statement = statement.where(Ticket.user_id == viewer["id"])
        counts = [count for _, count in db.execute(statement).all()]
    return {"unread_messages": sum(counts), "unread_tickets": len(counts)}


def mark_read(*, ticket_id: int, viewer: dict, last_message_id: int) -> dict:
    """原子 GREATEST 更新游标；旧标签页的迟到请求不会让已读状态倒退。"""
    with session_scope() as db:
        authorize(db, ticket_id, viewer)
        if last_message_id:
            message = db.get(TicketMessage, last_message_id)
            if message is None or message.ticket_id != ticket_id:
                raise ValidationError("已读消息不属于当前工单")
        statement = insert(TicketRead).values(ticket_id=ticket_id, user_id=viewer["id"], last_message_id=last_message_id)
        db.execute(statement.on_duplicate_key_update(last_message_id=func.greatest(TicketRead.last_message_id, statement.inserted.last_message_id)))
        cursor = db.scalar(select(TicketRead.last_message_id).where(TicketRead.ticket_id == ticket_id, TicketRead.user_id == viewer["id"]))
        remaining = dict(db.execute(unread_query(viewer["id"]).where(TicketMessage.ticket_id == ticket_id)).all())
    return {"last_message_id": cursor, "unread_count": int(remaining.get(ticket_id, 0))}
