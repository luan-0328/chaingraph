"""订单查询工具。

工具层只做两件事：
    1. 把业务 Service 的结果包装成结构化返回值；
    2. 把业务异常转成 {"success": false, "error": CODE}（v2.md §64：不要把堆栈扔给模型）。

**绝不直接操作数据库**（v2.md §37）：Tool → Service → Repository → MySQL。
"""

from langchain_core.tools import tool

from app.core.exceptions import BusinessError
from app.core.logging import get_logger
from app.services import order_service

logger = get_logger(__name__)


@tool
def cancel_order(order_id: int, user_id: int) -> dict:
    """取消当前用户的未支付订单，由订单服务校验归属和状态并回补库存。"""
    try:
        order = order_service.cancel_order(order_id=order_id, user_id=user_id)
    except BusinessError as exc:
        return exc.to_dict()
    return {"success": True, "executed": True,
            "order": {key: order[key] for key in ("id", "order_no", "payment_status", "fulfillment_status")}}


@tool
def query_order(order_id: int, user_id: int) -> dict:
    """查询订单详情：订单状态、金额、商品明细、支付记录、物流信息。

    参数：
        order_id: 订单 ID
        user_id:  当前用户 ID（用于归属校验，工具不会绕过权限）

    返回：
        成功 → {"success": true, "data": {order_no, total_amount, payment_status,
                fulfillment_status, after_sale_status, items, payments, logistics}}
        失败 → {"success": false, "error": "ORDER_NOT_FOUND" / "FORBIDDEN", "message": "..."}
    """
    try:
        detail = order_service.get_order_detail(order_id, user_id=user_id)
    except BusinessError as exc:
        logger.info("工具 query_order 业务失败：%s %s", exc.code, exc.message)
        return exc.to_dict()

    # 给模型看的数据要精简（物流里的大字段、内部 id 没必要全塞）
    return {
        "success": True,
        "data": {
            "order_id": detail["id"],
            "order_no": detail["order_no"],
            "total_amount": detail["total_amount"],
            "payment_status": detail["payment_status"],
            "fulfillment_status": detail["fulfillment_status"],
            "after_sale_status": detail["after_sale_status"],
            "created_at": str(detail["created_at"]),
            "paid_at": str(detail["paid_at"]) if detail["paid_at"] else None,
            "shipped_at": str(detail["shipped_at"]) if detail["shipped_at"] else None,
            "items": [
                {
                    "product_name": item["product_name"],
                    "product_price": item["product_price"],
                    "quantity": item["quantity"],
                    "subtotal": item["subtotal"],
                }
                for item in detail.get("items") or []
            ],
            "logistics_status": (detail.get("logistics") or {}).get("status"),
        },
    }


@tool
def query_order_items(order_id: int, user_id: int) -> dict:
    """查询订单的商品明细（下单时的商品快照）。"""
    try:
        detail = order_service.get_order_detail(order_id, user_id=user_id)
    except BusinessError as exc:
        return exc.to_dict()

    return {
        "success": True,
        "data": [
            {
                "product_name": item["product_name"],
                "product_price": item["product_price"],
                "quantity": item["quantity"],
                "subtotal": item["subtotal"],
            }
            for item in detail.get("items") or []
        ],
    }
