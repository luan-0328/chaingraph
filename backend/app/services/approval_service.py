"""售后页和工单页共用的审批入口。活动 Agent 的审批始终回到原任务。"""
from sqlalchemy import select

from app.core.database import session_scope
from app.core.execution import execution_session
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.ticket import Ticket, TicketStatus
from app.services import agent_task_service as tasks


def _related(db, order_id):
    """按固定顺序锁定订单关联工单及活动任务，多个活动方案时拒绝猜测。"""
    tickets = db.scalars(select(Ticket).where(Ticket.order_id == order_id)
                         .order_by(Ticket.id).with_for_update()).all()
    active = []
    for ticket in tickets:
        if ticket.agent_run_id:
            run = db.scalar(select(AgentRun).where(AgentRun.id == ticket.agent_run_id).with_for_update())
            if run and run.status in AgentRunStatus.ACTIVE:
                active.append(run)
    if len(active) > 1:
        tasks.conflict("该订单存在多个活动工单，请到工单页分别核实后处理")
    return tickets, active[0] if active else None


def get_order_approval_context(order_id: int) -> dict:
    """返回售后页审批所需的任务轮次和当前业务快照。"""
    with session_scope() as db:
        _, run = _related(db, order_id)
        return {"expected_run_id": run.id if run else None,
                "review_round": run.review_round if run else None,
                "ticket_id": run.ticket_id if run else None,
                "run_status": run.status if run else None,
                "review_payload": run.review_payload if run else None,
                "order_snapshot": tasks.order_review_snapshot(db, order_id)}


def decide_order_refund(*, order_id: int, operator_id: int, approved: bool, payload) -> None:
    """核对售后与订单快照，有活动 Agent 时将决定提交给原任务。"""
    from app.services import refund_service, ticket_service
    from app.repositories import after_sale_repository
    review_changed = False
    with session_scope() as db:
        tickets, run = _related(db, order_id)
        snapshot = tasks.order_review_snapshot(db, order_id)
        sale = after_sale_repository.get_by_id(db, payload.after_sale_id)
        if (not sale or sale["order_id"] != order_id or sale["type"] != "REFUND"
                or sale["status"] not in ("APPLYING", "PROCESSING", "APPROVED")):
            tasks.conflict("售后单已变化或不支持在线退款，请刷新")
        if snapshot != payload.order_snapshot:
            tasks.conflict("订单或售后信息已变化，请刷新并重新审核")
        token = execution_session.set(db)
        try:
            if run:
                if payload.expected_run_id != run.id or payload.review_round != run.review_round:
                    tasks.conflict("关联审批任务已变化，请刷新")
                if (run.review_payload or {}).get("business_action") != "apply_refund":
                    tasks.conflict("关联工单不是退款审批方案，请到工单页核实")
                # 与工单页完全相同的决定提交，不绕过 Graph 直接退款。
                submitted = tasks.submit_approval(ticket_id=run.ticket_id, operator_id=operator_id,
                    approved=approved, expected_run_id=run.id, review_round=payload.review_round,
                    comment=payload.comment)
                review_changed = submitted.get("review_changed", False)
            else:
                if payload.expected_run_id is not None:
                    tasks.conflict("原 Agent 审批已结束，不能将旧决定改为直接退款")
                if approved:
                    refund_service.execute_approved_refund(order_id=order_id,
                        operator_id=operator_id, comment=payload.comment)
                    # 仅同步明确关联此售后的未结束工单，不关闭该订单的其他咨询。
                    for ticket in tickets:
                        if ticket.after_sale_id == sale["id"] and ticket.status not in ("CLOSED", "RESOLVED"):
                            ticket_service.resolve_ticket(ticket_id=ticket.id,
                                solution="客服已批准全额退款，退款处理完成", operator_id=operator_id)
                else:
                    refund_service.reject_refund(order_id=order_id, operator_id=operator_id,
                                                 reason=payload.comment)
                    for ticket in tickets:
                        if ticket.after_sale_id == sale["id"] and ticket.status not in ("CLOSED", "RESOLVED"):
                            ticket.status = TicketStatus.PROCESSING
                            ticket_service.add_message(ticket_id=ticket.id, sender_type="SYSTEM",
                                sender_id=operator_id, content=f"售后申请被驳回：{payload.comment or '请联系客服'}")
        finally:
            execution_session.reset(token)
    if review_changed:
        tasks.conflict("订单状态已变化，已更新审批版本，请刷新后重新审核")
