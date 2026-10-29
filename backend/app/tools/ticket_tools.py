"""工单工具：查询历史工单 / 更新工单信息。"""

from langchain_core.tools import tool

from app.core.exceptions import BusinessError
from app.core.logging import get_logger
from app.services import ticket_service

logger = get_logger(__name__)


@tool
def query_ticket_history(user_id: int, limit: int = 5) -> dict:
    """查询该用户最近的工单（长期记忆的轻量实现：让 Agent 知道"上次也问过同类问题"）。"""
    result = ticket_service.list_my_tickets(user_id=user_id, limit=limit)
    return {
        "success": True,
        "data": [
            {
                "ticket_no": item["ticket_no"],
                "title": item["title"],
                "type": item["type"],
                "status": item["status"],
                "created_at": str(item["created_at"]),
            }
            for item in result["items"]
        ],
        "total": result["total"],
    }


@tool
def update_ticket(
    ticket_id: int,
    type: str | None = None,
    priority: str | None = None,
    title: str | None = None,
) -> dict:
    """更新工单的分类/优先级/标题（Agent 分析出问题类型后回填，便于客服检索）。"""
    fields: dict = {}
    if type is not None:
        fields["type"] = type
    if priority is not None:
        fields["priority"] = priority
    if title is not None:
        fields["title"] = title

    if not fields:
        return {"success": False, "error": "NO_FIELDS", "message": "没有需要更新的字段"}

    try:
        ticket = ticket_service.update_ticket_fields(ticket_id, **fields)
    except BusinessError as exc:
        return exc.to_dict()

    return {
        "success": True,
        "data": {
            "ticket_id": ticket["id"],
            "type": ticket["type"],
            "priority": ticket["priority"],
            "title": ticket["title"],
        },
    }
