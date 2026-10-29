"""用户长期记忆（v2.md §41.2）。

V1 不做向量化长期记忆，只做"结构化事实"：
    FACT     用户偏好/习惯（例如"只接受顺丰"）
    SUMMARY  历史工单摘要（例如"30 天内申请过 3 次退款"）
    NOTE     客服备注

写入时机：TicketService 结单时在同一事务中保存有来源的处理摘要。
读取时机：生成处理方案前通过 query_user_memory 读取最近三条有效记忆。
"""

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.models.user_memory import MemoryType
from app.repositories import user_memory_repository

logger = get_logger(__name__)


def remember(
    *,
    user_id: int,
    content: str,
    memory_type: str = MemoryType.FACT,
    source: str | None = None,
) -> dict:
    """写入一条用户记忆。"""
    with session_scope() as db:
        memory = user_memory_repository.create(
            db, user_id=user_id, content=content, memory_type=memory_type, source=source
        )
    logger.info("写入用户记忆：user=%s type=%s", user_id, memory_type)
    return memory


def recall(user_id: int, *, limit: int = 10, exclude_ticket_id: int | None = None) -> list[dict]:
    """读取有界的用户记忆，排除当前工单以及已重新打开的工单摘要。"""
    with session_scope() as db:
        return user_memory_repository.list_by_user(db, user_id, limit=limit,
                                                   exclude_ticket_id=exclude_ticket_id)


def forget(memory_id: int, *, user_id: int) -> bool:
    """校验归属后删除记忆，不删除原始工单和审计。"""
    with session_scope() as db:
        memory = user_memory_repository.get_by_id(db, memory_id)
        if memory is None:
            raise NotFoundError("记忆不存在")
        if memory["user_id"] != user_id:
            raise PermissionDeniedError("无权删除他人的记忆")
        return user_memory_repository.delete(db, memory_id)


def summarize_refund_history(user_id: int) -> str | None:
    """按需生成一条"退款历史"摘要（供 Agent 判断频繁退款风险）。"""
    from app.core.database import session_scope as _scope
    from app.repositories import refund_repository

    with _scope() as db:
        refunds, total = refund_repository.list_all(db, limit=1, user_id=user_id)
    if not refunds:
        return None
    return f"该用户历史退款记录 {total} 笔（包含各状态，最近一笔状态：{refunds[0]['status']}）"
