"""客服工作台接口（v2.md §23-§25、§45）。

全部接口都要求 CUSTOMER_SERVICE 或 ADMIN 角色 —— 权限在后端再校验一遍，
不能只靠前端隐藏菜单（v2.md §58）。
"""

from fastapi import APIRouter, Depends, Query

from app.api import mappers
from app.core.database import session_scope
from app.core.dependencies import require_roles
from app.core.logging import get_logger
from app.models.after_sale import AfterSaleStatus
from app.models.ticket import TicketStatus
from app.models.ticket_message import SenderType
from app.models.user import Role
from app.repositories import agent_run_repository, product_repository, user_repository
from app.schemas.admin import AdminUserListResponse, AdminUserInfo, DashboardStats
from app.schemas.after_sale import AfterSaleInfo, AfterSaleListResponse, RefundInfo, RefundApprovalRequest
from app.services import approval_service
from app.schemas.order import OrderDetail, OrderListResponse, OrderSummary
from app.schemas.ticket import (
    TicketAssignRequest,
    TicketCloseRequest,
    TicketDetail,
    TicketListResponse,
    TicketMessageCreate,
    TicketMessageInfo,
    TicketResolveRequest,
    TicketSummary,
)
from app.services import (
    after_sale_service,
    logistics_service,
    order_service,
    refund_service,
    ticket_service,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/customer-service", tags=["customer-service"])

staff_only = require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN)


# ======================================================================
# 数据概览（工作台首页）
# ======================================================================
@router.get("/dashboard", response_model=DashboardStats, summary="工作台数据概览")
def dashboard(current_user: dict = Depends(staff_only)) -> DashboardStats:
    """客服工作台首页数据（工单按状态分布、订单/售后/Agent 概况）。"""
    ticket_stats = ticket_service.get_stats()
    ticket_total = ticket_service.list_tickets_for_staff(limit=1)["total"]
    order_total = order_service.list_all_orders(limit=1)["total"]
    order_paid = order_service.list_all_orders(limit=1, payment_status="PAID")["total"]
    after_sale_pending = after_sale_service.list_after_sales(
        limit=1, status=AfterSaleStatus.APPLYING
    )["total"]

    with session_scope() as db:
        agent_stats = agent_run_repository.count_by_status(db)
        product_on_sale = product_repository.list_products(db, limit=1, only_on_sale=True)[1]

    return DashboardStats(
        ticket_total=ticket_total,
        ticket_by_status=ticket_stats,
        order_total=order_total,
        order_paid=order_paid,
        after_sale_pending=after_sale_pending,
        agent_run_by_status=agent_stats,
        product_on_sale=product_on_sale,
    )


# ======================================================================
# 工单
# ======================================================================
@router.get("/tickets", response_model=TicketListResponse, summary="工单列表（全部）")
def list_tickets(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    only_mine: bool = Query(default=False, description="只看我领取的"),
    unassigned: bool = Query(default=False, description="只看未领取的"),
    waiting_human: bool = Query(default=False, description="只看待人工确认的"),
    keyword: str | None = Query(default=None, max_length=100),
    current_user: dict = Depends(staff_only),
) -> TicketListResponse:
    """工单列表：客服可以按状态、领取情况、关键词筛选。"""
    statuses = (TicketStatus.WAITING_HUMAN,) if waiting_human else None
    result = ticket_service.list_tickets_for_staff(
        limit=limit,
        offset=offset,
        status=status_filter,
        statuses=statuses,
        assignee_id=current_user["id"] if only_mine else None,
        unassigned=unassigned,
        keyword=keyword,
    )
    return TicketListResponse(
        items=[TicketSummary(**mappers.ticket_summary(t)) for t in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/tickets/{ticket_id}", response_model=TicketDetail, summary="工单详情（含用户/订单/Agent）")
def get_ticket(
    ticket_id: int, current_user: dict = Depends(staff_only)
) -> TicketDetail:
    """客服视角：不做归属校验（客服本来就要看全部工单），详情里带 Agent 分析与事件。"""
    ticket = ticket_service.get_ticket_detail(ticket_id)
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post("/tickets/{ticket_id}/assign", response_model=TicketDetail, summary="领取/指派工单")
def assign_ticket(
    ticket_id: int,
    payload: TicketAssignRequest | None = None,
    current_user: dict = Depends(staff_only),
) -> TicketDetail:
    """客服领取工单（不传 assignee_id = 自己领取；传 null = 取消领取）。"""
    fields = payload.model_dump(exclude_unset=True) if payload else {}
    assignee_id = fields.get("assignee_id", current_user["id"])
    ticket_service.assign_ticket(ticket_id=ticket_id, assignee_id=assignee_id)
    return TicketDetail(**mappers.ticket_detail(ticket_service.get_ticket_detail(ticket_id)))


@router.post("/tickets/{ticket_id}/messages", response_model=TicketMessageInfo, summary="客服回复")
def reply_ticket(
    ticket_id: int,
    payload: TicketMessageCreate,
    current_user: dict = Depends(staff_only),
) -> TicketMessageInfo:
    message = ticket_service.add_message(
        ticket_id=ticket_id,
        content=payload.content,
        sender_type=SenderType.CUSTOMER_SERVICE,
        sender_id=current_user["id"],
    )
    return TicketMessageInfo(**mappers.message_info(message))


@router.post("/tickets/{ticket_id}/resolve", response_model=TicketDetail, summary="标记已解决")
def resolve_ticket(
    ticket_id: int,
    payload: TicketResolveRequest,
    current_user: dict = Depends(staff_only),
) -> TicketDetail:
    ticket = ticket_service.resolve_ticket(
        ticket_id=ticket_id, solution=payload.solution, operator_id=current_user["id"]
    )
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post("/tickets/{ticket_id}/close", response_model=TicketDetail, summary="关闭工单")
def close_ticket(
    ticket_id: int,
    payload: TicketCloseRequest | None = None,
    current_user: dict = Depends(staff_only),
) -> TicketDetail:
    ticket = ticket_service.close_ticket(
        ticket_id=ticket_id,
        comment=payload.comment if payload else None,
        operator_id=current_user["id"],
    )
    return TicketDetail(**mappers.ticket_detail(ticket))


# ======================================================================
# 订单（客服视角）
# ======================================================================
@router.get("/orders", response_model=OrderListResponse, summary="订单列表（全部）")
def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None, max_length=64),
    payment_status: str | None = Query(default=None),
    fulfillment_status: str | None = Query(default=None),
    current_user: dict = Depends(staff_only),
) -> OrderListResponse:
    result = order_service.list_all_orders(
        limit=limit,
        offset=offset,
        keyword=keyword,
        payment_status=payment_status,
        fulfillment_status=fulfillment_status,
    )
    return OrderListResponse(
        items=[OrderSummary(**mappers.order_summary(o)) for o in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/orders/{order_id}", response_model=OrderDetail, summary="订单详情（客服视角）")
def get_order(order_id: int, current_user: dict = Depends(staff_only)) -> OrderDetail:
    order = order_service.get_order_detail(order_id)
    return OrderDetail(**mappers.order_detail(order))


@router.post("/orders/{order_id}/ship", response_model=OrderDetail, summary="确认发货")
def ship_order(order_id: int, current_user: dict = Depends(staff_only)) -> OrderDetail:
    """模拟发货：生成运单号 + 订单转 SHIPPED。"""
    logistics_service.ship_order(order_id=order_id, operator_id=current_user["id"])
    return OrderDetail(**mappers.order_detail(order_service.get_order_detail(order_id)))


@router.post("/orders/{order_id}/deliver", response_model=OrderDetail, summary="模拟签收")
def deliver_order(order_id: int, current_user: dict = Depends(staff_only)) -> OrderDetail:
    logistics_service.deliver_order(order_id=order_id)
    return OrderDetail(**mappers.order_detail(order_service.get_order_detail(order_id)))


@router.post("/orders/{order_id}/transit", response_model=OrderDetail, summary="模拟运输中")
def transit_order(
    order_id: int,
    message: str = Query(default="快件已到达转运中心"),
    current_user: dict = Depends(staff_only),
) -> OrderDetail:
    logistics_service.update_transit(order_id=order_id, message=message)
    return OrderDetail(**mappers.order_detail(order_service.get_order_detail(order_id)))


# ======================================================================
# 售后 / 退款审批（HITL 的"人工确认"落地点）
# ======================================================================
@router.get("/after-sales", response_model=AfterSaleListResponse, summary="售后列表（全部）")
def list_after_sales(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    order_id: int | None = Query(default=None),
    current_user: dict = Depends(staff_only),
) -> AfterSaleListResponse:
    result = after_sale_service.list_after_sales(
        limit=limit, offset=offset, status=status_filter, order_id=order_id
    )
    return AfterSaleListResponse(
        items=[AfterSaleInfo(**mappers.after_sale_info(s)) for s in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/after-sales/{after_sale_id}", response_model=AfterSaleInfo, summary="售后详情")
def get_after_sale(after_sale_id: int, current_user: dict = Depends(staff_only)) -> AfterSaleInfo:
    sale = after_sale_service.get_after_sale(after_sale_id)
    sale["approval_context"] = approval_service.get_order_approval_context(sale["order_id"])
    return AfterSaleInfo(**mappers.after_sale_info(sale))


@router.post("/orders/{order_id}/refund/approve", response_model=OrderDetail, summary="批准退款")
def approve_refund(
    order_id: int,
    payload: RefundApprovalRequest,
    current_user: dict = Depends(staff_only),
) -> OrderDetail:
    """人工批准退款：执行退款 + 售后单完成 + 订单置 REFUNDED（一个事务）。"""
    approval_service.decide_order_refund(order_id=order_id, operator_id=current_user["id"],
                                        approved=True, payload=payload)
    return OrderDetail(**mappers.order_detail(order_service.get_order_detail(order_id)))


@router.post("/orders/{order_id}/refund/reject", response_model=OrderDetail, summary="拒绝退款")
def reject_refund(
    order_id: int,
    payload: RefundApprovalRequest,
    current_user: dict = Depends(staff_only),
) -> OrderDetail:
    approval_service.decide_order_refund(order_id=order_id, operator_id=current_user["id"],
                                        approved=False, payload=payload)
    return OrderDetail(**mappers.order_detail(order_service.get_order_detail(order_id)))


@router.get("/refunds", summary="退款流水列表")
def list_refunds(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: dict = Depends(staff_only),
) -> dict:
    result = refund_service.list_refunds(limit=limit, offset=offset, status=status_filter)
    return {
        "items": [RefundInfo(**mappers.refund_info(r)) for r in result["items"]],
        "total": result["total"],
        "limit": result["limit"],
        "offset": result["offset"],
    }


# ======================================================================
# 用户查询
# ======================================================================
@router.get("/users", response_model=AdminUserListResponse, summary="用户列表")
def list_users(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None, max_length=64),
    role: str | None = Query(default=None),
    current_user: dict = Depends(staff_only),
) -> AdminUserListResponse:
    with session_scope() as db:
        items, total = user_repository.list_users(
            db, limit=limit, offset=offset, keyword=keyword, role=role
        )
    return AdminUserListResponse(
        items=[AdminUserInfo(**mappers.admin_user_info(u)) for u in items],
        total=total,
        limit=limit,
        offset=offset,
    )
