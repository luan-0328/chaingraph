"""用户长期记忆数据访问。"""

from sqlalchemy import LargeBinary, String, cast, exists, literal, or_, select
from sqlalchemy.orm import Session

from app.models.user_memory import UserMemory
from app.models.ticket import Ticket, TicketStatus


def create(
    db: Session,
    *,
    user_id: int,
    content: str,
    memory_type: str = "FACT",
    source: str | None = None,
) -> dict:
    """写入一条用户长期记忆。

    `memory_type` 区分事实（FACT：用户偏好/会员等级）与偏好类记忆；
    `source` 记下这条记忆是哪来的（哪张工单总结出来的），便于追溯与清理 ——
    没有来源的记忆过一阵子就没人敢用了。
    """
    memory = UserMemory(user_id=user_id, content=content, memory_type=memory_type, source=source)
    db.add(memory)
    db.flush()
    db.refresh(memory)
    return memory.to_dict()


def list_by_user(db: Session, user_id: int, *, limit: int = 10,
                 exclude_ticket_id: int | None = None) -> list[dict]:
    """取某用户最近的 N 条记忆（倒序）。

    limit 默认 10：记忆会拼进 Agent 的 prompt，全量塞进去既贵又会稀释
    "最近/最相关"的那几条。当前没有做相关性排序，只按新→旧取。
    """
    valid_ticket = exists(select(Ticket.id).where(
        # 按来源键字节匹配，避免表和连接采用不同 utf8mb4 collation 时比较报错。
        cast(UserMemory.source, LargeBinary) == cast(literal("ticket:") + cast(Ticket.id, String), LargeBinary),
        Ticket.user_id == user_id,
        Ticket.status.in_((TicketStatus.RESOLVED, TicketStatus.CLOSED)),
    ))
    conditions = [UserMemory.user_id == user_id,
                  or_(UserMemory.source.is_(None), ~UserMemory.source.startswith("ticket:"), valid_ticket)]
    if exclude_ticket_id is not None:
        conditions.append(or_(UserMemory.source.is_(None), UserMemory.source != f"ticket:{exclude_ticket_id}"))
    rows = db.scalars(
        select(UserMemory)
        .where(*conditions)
        .order_by(UserMemory.updated_at.desc(), UserMemory.id.desc())
        .limit(max(1, min(limit, 20)))
    )
    return [memory.to_dict() for memory in rows]


def save_ticket_summary(db: Session, *, ticket: dict, solution: str) -> dict:
    """调用方持有工单行锁，同一工单只维护一条摘要，与结单事务一起提交。"""
    source = f"ticket:{ticket['id']}"
    content = (f"历史工单 {ticket['ticket_no']}，关联订单 {ticket.get('order_id') or '无'}。"
               f"用户诉求：{ticket['content'][:120]}。"
               f"当时记录的处理结论（不代表当前订单状态）：{solution[:320]}")
    memory = db.scalar(select(UserMemory).where(
        UserMemory.user_id == ticket["user_id"], UserMemory.source == source))
    if memory is None:
        return create(db, user_id=ticket["user_id"], content=content,
                      memory_type="SUMMARY", source=source)
    memory.content = content
    memory.memory_type = "SUMMARY"
    db.flush()
    db.refresh(memory)
    return memory.to_dict()


def delete(db: Session, memory_id: int) -> bool:
    """删除一条记忆，返回是否删到了（False = 已经不存在）。

    删除后不再参与后续记忆查询；工单审计和已有 Checkpoint 另行保留。
    """
    memory = db.get(UserMemory, memory_id)
    if memory is None:
        return False
    db.delete(memory)
    db.flush()
    return True


def get_by_id(db: Session, memory_id: int) -> dict | None:
    """按 ID 读取，删除前由业务层检查归属。"""
    memory = db.get(UserMemory, memory_id)
    return memory.to_dict() if memory else None
