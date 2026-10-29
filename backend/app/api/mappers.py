"""DTO 映射：把 Service 返回的业务字典转成接口契约需要的形状。

为什么需要这一层？
    数据库主键字段在模型里统一叫 `id`（表设计规范），
    而接口对前端暴露的是 `ticket_id` / `order_id` / `product_id` 这种带语义的名字。
    与其让 Service 迁就前端命名，不如在 API 边界做一次显式映射 —— 契约清晰、改动可控。
"""

from typing import Any


def _get(data: dict, *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


# ======================================================================
# 用户 / 商品
# ======================================================================
def user_info(user: dict) -> dict:
    return {
        "user_id": user["id"],
        "username": user["username"],
        "nickname": user.get("nickname"),
        "email": user.get("email"),
        "phone": user.get("phone"),
        "role": user.get("role", "USER"),
        "created_at": user.get("created_at"),
    }


def admin_user_info(user: dict) -> dict:
    return {
        "user_id": user["id"],
        "username": user["username"],
        "nickname": user.get("nickname"),
        "role": user.get("role"),
        "status": user.get("status"),
        "created_at": user.get("created_at"),
    }


def product_info(product: dict) -> dict:
    return {
        "product_id": product["id"],
        "name": product["name"],
        "description": product.get("description"),
        "price": product.get("price"),
        "stock": product.get("stock"),
        "status": product.get("status"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
    }


# ======================================================================
# 购物车
# ======================================================================
def cart_item_info(item: dict) -> dict:
    """Service 返回的购物车行已经是接口形状（见 cart_service._enrich）。"""
    return item


# ======================================================================
# 订单 / 支付 / 物流
# ======================================================================
def order_summary(order: dict) -> dict:
    return {
        "order_id": order["id"],
        "order_no": order["order_no"],
        "total_amount": order.get("total_amount"),
        "payment_status": order.get("payment_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "after_sale_status": order.get("after_sale_status"),
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
    }


def order_item_info(item: dict) -> dict:
    return {
        "product_id": item["product_id"],
        "product_name": item["product_name"],
        "product_price": item.get("product_price"),
        "quantity": item.get("quantity"),
        "subtotal": item.get("subtotal"),
    }


def payment_info(payment: dict) -> dict:
    return {
        "payment_no": payment["payment_no"],
        "amount": payment.get("amount"),
        "status": payment.get("status"),
        "paid_at": payment.get("paid_at"),
    }


def logistics_info(record: dict) -> dict:
    return {
        "tracking_no": record["tracking_no"],
        "company": record.get("company"),
        "status": record.get("status"),
        "latest_message": record.get("latest_message"),
        "last_update_time": record.get("last_update_time"),
    }


def order_detail(order: dict) -> dict:
    return {
        **order_summary(order),
        "items": [order_item_info(item) for item in order.get("items") or []],
        "payments": [payment_info(payment) for payment in order.get("payments") or []],
        "logistics": logistics_info(order["logistics"]) if order.get("logistics") else None,
    }


# ======================================================================
# 售后 / 退款
# ======================================================================
def refund_info(refund: dict) -> dict:
    return {
        "refund_no": refund["refund_no"],
        "amount": refund.get("amount"),
        "status": refund.get("status"),
        "reason": refund.get("reason"),
        "approved_by": refund.get("approved_by"),
        "created_at": refund.get("created_at"),
        "completed_at": refund.get("completed_at"),
    }


def after_sale_info(sale: dict) -> dict:
    return {
        "approval_context": sale.get("approval_context"),
        "after_sale_id": sale["id"],
        "after_sale_no": sale["after_sale_no"],
        "order_id": sale["order_id"],
        "type": sale.get("type"),
        "status": sale.get("status"),
        "amount": sale.get("amount"),
        "reason": sale.get("reason"),
        "refunds": [refund_info(r) for r in sale.get("refunds") or []],
        "created_at": sale.get("created_at"),
        "updated_at": sale.get("updated_at"),
        "completed_at": sale.get("completed_at"),
    }


# ======================================================================
# 工单 / 消息 / Agent
# ======================================================================
def message_info(message: dict) -> dict:
    return {
        "message_id": message["id"],
        "ticket_id": message["ticket_id"],
        "sender_type": message.get("sender_type"),
        "sender_id": message.get("sender_id"),
        "content": message.get("content"),
        "created_at": message.get("created_at"),
    }


def event_info(event: dict) -> dict:
    return {
        "event_type": event.get("event_type"),
        "message": event.get("message"),
        "created_at": event.get("created_at"),
    }


def agent_run_info(run: dict) -> dict:
    return {
        "review_round": run.get("review_round", 0),
        "review_payload": run.get("review_payload"),
        "execution_epoch": run.get("execution_epoch", 0),
        "lease_until": run.get("lease_until"),
        "agent_run_id": run["id"],
        "thread_id": run.get("thread_id"),
        "status": run.get("status"),
        "used_rag": bool(run.get("used_rag")),
        "used_tool": bool(run.get("used_tool")),
        "need_human": bool(run.get("need_human")),
        "retry_count": int(run.get("retry_count") or 0),
        "resumed": bool(run.get("resumed")),
        "current_node": run.get("current_node"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "alive_at": run.get("alive_at"),
        "error": run.get("error"),
        "decision": run.get("decision"),
        "result": run.get("result"),
    }


def ticket_summary(ticket: dict) -> dict:
    return {
        "ticket_id": ticket["id"],
        "ticket_no": ticket["ticket_no"],
        "user_id": ticket["user_id"],
        "title": ticket.get("title"),
        "content": ticket.get("content"),
        "type": ticket.get("type"),
        "priority": ticket.get("priority"),
        "status": ticket.get("status"),
        # Agent 执行权状态（同工单互斥的依据）—— 注意与 status 含义不同
        "agent_status": ticket.get("agent_status") or "IDLE",
        "agent_run_id": ticket.get("agent_run_id"),
        "assignee_id": ticket.get("assignee_id"),
        "order_id": ticket.get("order_id"),
        "after_sale_id": ticket.get("after_sale_id"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
    }


def ticket_detail(ticket: dict) -> dict:
    return {
        **ticket_summary(ticket),
        "messages": [message_info(m) for m in ticket.get("messages") or []],
        "agent_run": agent_run_info(ticket["agent_run"]) if ticket.get("agent_run") else None,
        "agent_events": [event_info(e) for e in ticket.get("agent_events") or []],
        "agent_runs": [agent_run_info(r) for r in ticket.get("agent_runs") or []],
    }


# ======================================================================
# 知识库
# ======================================================================
def knowledge_info(document: dict) -> dict:
    return {
        "document_id": document["id"],
        "title": document.get("title"),
        "content": document.get("content"),
        "category": document.get("category"),
        "source": document.get("source"),
        "version": document.get("version"),
        "status": document.get("status"),
        # 分块数：0 说明这篇还没发布过（前端据此显示"发布/重新发布"按钮）
        "chunk_count": document.get("chunk_count", 0),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
    }
