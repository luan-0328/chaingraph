"""用户工具：查询用户信息与用户维度记忆。"""

from langchain_core.tools import tool

from app.core.exceptions import BusinessError
from app.core.logging import get_logger
from app.memory import user_memory

logger = get_logger(__name__)


@tool
def query_user(user_id: int) -> dict:
    """查询用户基本信息（昵称、角色）—— 用于称呼与判断身份。"""
    from app.services import auth_service

    try:
        user = auth_service.get_profile(user_id)
    except BusinessError as exc:
        return exc.to_dict()

    return {
        "success": True,
        "data": {
            "user_id": user["id"],
            "username": user["username"],
            "nickname": user.get("nickname"),
            "role": user.get("role"),
        },
    }


@tool
def query_user_memory(user_id: int, limit: int = 5, exclude_ticket_id: int | None = None) -> dict:
    """查询该用户的长期记忆（客服备注、历史事实），例如"该用户多次申请退款"。"""
    memories = user_memory.recall(user_id, limit=limit, exclude_ticket_id=exclude_ticket_id)

    return {
        "success": True,
        "data": [
            {"type": item["memory_type"], "content": item["content"][:600],
             "source": item["source"]} for item in memories
        ],
    }
