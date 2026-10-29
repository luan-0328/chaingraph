"""物流查询工具。"""

from langchain_core.tools import tool

from app.core.config import settings
from app.core.exceptions import BusinessError
from app.core.logging import get_logger
from app.core.time import hours_since
from app.services import logistics_service

logger = get_logger(__name__)


@tool
def query_logistics(order_id: int, user_id: int) -> dict:
    """查询订单物流：承运商、运单号、当前状态、最新动态、距上次更新多久。

    额外算出 hours_since_update / is_stalled，
    方便判断"物流是否停滞"（规则见知识库 logistics_policy.md：超过 72 小时视为异常）。
    """
    try:
        record = logistics_service.get_logistics(order_id, user_id=user_id)
    except BusinessError as exc:
        logger.info("工具 query_logistics 业务失败：%s %s", exc.code, exc.message)
        return exc.to_dict()

    stalled_hours = hours_since(record.get("last_update_time"))
    return {
        "success": True,
        "data": {
            "order_id": record["order_id"],
            "company": record.get("company"),
            "tracking_no": record.get("tracking_no"),
            "status": record.get("status"),
            "latest_message": record.get("latest_message"),
            "last_update_time": str(record.get("last_update_time") or ""),
            "hours_since_update": stalled_hours,
            "is_stalled": bool(
                stalled_hours is not None and stalled_hours > settings.logistics_stalled_hours
            ),
        },
    }
