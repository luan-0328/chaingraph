"""售后工具：查询售后单 / 创建售后单。"""

from langchain_core.tools import tool

from app.core.exceptions import BusinessError
from app.core.logging import get_logger
from app.services import after_sale_service

logger = get_logger(__name__)


@tool
def query_after_sale(order_id: int, user_id: int) -> dict:
    """查询某个订单的售后单与退款流水（判断"是不是已经在退了"）。"""
    try:
        # 用户视角查询：只返回自己的售后单
        result = after_sale_service.list_after_sales(user_id=user_id, order_id=order_id)
    except BusinessError as exc:
        return exc.to_dict()

    items = [
        {
            "after_sale_id": item["id"],
            "after_sale_no": item["after_sale_no"],
            "type": item["type"],
            "status": item["status"],
            "amount": item["amount"],
            "reason": item["reason"],
            "created_at": str(item["created_at"]),
            "completed_at": str(item["completed_at"]) if item.get("completed_at") else None,
        }
        for item in result["items"]
    ]
    return {"success": True, "data": items, "total": result["total"]}


@tool
def create_after_sale(order_id: int, user_id: int, reason: str, amount: float | None = None) -> dict:
    """为订单创建售后单（退款申请的业务载体）。

    什么时候用：用户明确要求售后退款，但系统里还没有对应的售后单。
    注意：创建售后单**不等于退款**，真正的退款由 apply_refund 执行并受 RefundService 校验。
    """
    try:
        sale = after_sale_service.create_after_sale(
            user_id=user_id, order_id=order_id, reason=reason, amount=amount
        )
    except BusinessError as exc:
        logger.info("工具 create_after_sale 业务失败：%s %s", exc.code, exc.message)
        return exc.to_dict()

    return {
        "success": True,
        "data": {
            "after_sale_id": sale["id"],
            "after_sale_no": sale["after_sale_no"],
            "status": sale["status"],
            "amount": sale["amount"],
        },
    }
