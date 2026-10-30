"""统一的人工交接入口；按钮、短指令和 Agent 能力不足都走同一事务规则。"""

import re
import unicodedata

from app.core.database import session_scope
from app.core.exceptions import InvalidStateError, PermissionDeniedError
from app.core.execution import assert_execution, execution_claim
from app.core.time import utcnow
from app.models.agent_run import AgentRunStatus
from app.models.ticket import AgentStatus, HandlingMode, TicketStatus
from app.models.ticket_message import SenderType
from app.repositories import agent_run_repository, order_repository, ticket_repository

_COMMAND = re.compile(
    r"(?:请|麻烦|帮我|请帮我|麻烦帮我)?"
    r"(?:转(?:接)?(?:到)?(?:人工(?:客服)?|真人客服)|人工客服|真人客服|"
    r"(?:我要|我想要|我需要|找|联系)(?:一位|个)?(?:人工客服|真人客服))"
    r"(?:一下|吧|谢谢|谢谢你)?"
)


def is_handoff_command(content: str) -> bool:
    """只匹配独立短指令；不使用包含匹配，避免否定句、引用和营业时间咨询误转接。"""
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", content))
    return _COMMAND.fullmatch(normalized.strip("!！?？。.,，~～")) is not None


def request_locked(db, ticket_id: int, reason: str) -> None:
    """持锁发起交接；RUNNING 只设置请求，不撤销执行凭证或伪造任务已经停止。"""
    from app.services.agent_task_service import lock_ticket_run
    ticket, run = lock_ticket_run(db, ticket_id)
    if ticket.handling_mode in (HandlingMode.HUMAN, HandlingMode.HUMAN_PENDING):
        return
    ticket.handoff_round += 1
    ticket.handoff_reason = reason[:500]
    ticket.handoff_requested_at = utcnow()
    ticket.resolved_at = None
    ticket.closed_at = None
    key = f"ticket:{ticket.id}:handoff:{ticket.handoff_round}"
    ticket_repository.add_message(db, ticket_id=ticket.id, sender_type=SenderType.SYSTEM,
        content=f"已收到转人工请求：{ticket.handoff_reason}。正在交接；已完成的业务操作不会被撤销。",
        dedup_key=f"{key}:requested")
    if run and run.status == AgentRunStatus.RUNNING:
        ticket.handling_mode = HandlingMode.HUMAN_PENDING
        ticket.status = TicketStatus.PROCESSING
        return
    finish_locked(db, ticket, run)


def finish_locked(db, ticket, run) -> None:
    """只有没有执行中的工具线程，或 worker 已确认图返回，才能完成交接。"""
    key = f"ticket:{ticket.id}:handoff:{ticket.handoff_round}"
    if run and run.status in AgentRunStatus.ACTIVE:
        run.status = AgentRunStatus.REJECTED
        run.error = "HUMAN_HANDOFF：" + (ticket.handoff_reason or "转人工处理")
        run.finished_at = utcnow()
        run.worker_id = None
        run.lease_until = None
        run.current_node = None
        agent_run_repository.add_event(db, agent_run_id=run.id, event_type="HUMAN_HANDOFF",
            message="自动执行已停止，后续由人工处理", dedup_key=f"{key}:done-event")
    ticket.handling_mode = HandlingMode.HUMAN
    ticket.agent_run_id = None
    ticket.agent_status = AgentStatus.IDLE
    ticket.status = TicketStatus.PROCESSING if ticket.assignee_id else TicketStatus.WAITING_HUMAN
    message = "已转入人工处理，客服可在本工单中继续交流；Agent 不会自动回复或执行操作。"
    if ticket.order_id:
        order = order_repository.get_model_for_update(db, ticket.order_id)
        if order and order.payment_status == "REFUNDED":
            message += "系统已记录该订单退款，请客服核实结果；转人工不会撤销已完成的退款。"
    ticket_repository.add_message(db, ticket_id=ticket.id, sender_type=SenderType.SYSTEM,
        content=message, dedup_key=f"{key}:ready")


def request_human(*, ticket_id: int, user_id: int, reason: str = "用户主动要求人工客服") -> dict:
    """用户只能转接自己的工单，重复请求不会重复建任务或写交接消息。"""
    from app.services.agent_task_service import lock_ticket_run
    from app.services.ticket_service import get_ticket_detail
    with session_scope() as db:
        ticket, _ = lock_ticket_run(db, ticket_id)
        if ticket.user_id != user_id:
            raise PermissionDeniedError("无权转接他人的工单")
        request_locked(db, ticket_id, reason)
    return get_ticket_detail(ticket_id, user_id=user_id)


def request_from_agent(*, ticket_id: int, reason: str, result: dict | None = None,
                       decision: dict | None = None) -> None:
    """保存 Agent 交接摘要；执行事务入口仍校验当前任务的租约与归属。"""
    claim = execution_claim.get()
    if claim is None or claim.ticket_id != ticket_id:
        raise InvalidStateError("只有当前执行任务可以提交 Agent 人工交接")
    from app.services.agent_task_service import lock_ticket_run
    with session_scope() as db:
        _, run = lock_ticket_run(db, ticket_id)
        if run is not None:
            run.result = result or run.result
            run.decision = decision or run.decision
        request_locked(db, ticket_id, reason)


def complete_worker_handoff(claim) -> None:
    """worker 退出图执行后确认交接，保留租约检查并允许本次收尾越过交接拦截。"""
    from app.services.agent_task_service import lock_ticket_run
    token = execution_claim.set(None)
    try:
        with session_scope() as db:
            assert_execution(db, claim, allow_handoff=True)
            ticket, run = lock_ticket_run(db, claim.ticket_id)
            finish_locked(db, ticket, run)
    finally:
        execution_claim.reset(token)


def return_to_agent(*, ticket_id: int, operator_id: int) -> dict:
    """只有客服明确交回后才允许新建 Agent 任务，不复活已经交接的旧 Checkpoint。"""
    from app.services.agent_task_service import lock_ticket_run
    from app.services.refund_service import _validate_operator
    from app.services.ticket_service import get_ticket_detail
    with session_scope() as db:
        _validate_operator(db, operator_id)
        ticket, run = lock_ticket_run(db, ticket_id)
        if ticket.handling_mode == HandlingMode.HUMAN_PENDING or (run and run.status in AgentRunStatus.ACTIVE):
            raise InvalidStateError("Agent 尚未确认交接，不能交回")
        if ticket.assignee_id != operator_id:
            raise PermissionDeniedError("只有当前负责客服可以交回 Agent，请先领取工单")
        if ticket.status in (TicketStatus.CLOSED, TicketStatus.RESOLVED):
            raise InvalidStateError("请先重新打开工单")
        if ticket.handling_mode != HandlingMode.AGENT:
            ticket.handling_mode = HandlingMode.AGENT
            ticket.status = TicketStatus.PROCESSING
            ticket_repository.add_message(db, ticket_id=ticket.id, sender_type=SenderType.SYSTEM,
                sender_id=operator_id, content="客服已明确交回 Agent，可发起新的自动处理任务",
                dedup_key=f"ticket:{ticket.id}:handoff:{ticket.handoff_round}:returned")
    return get_ticket_detail(ticket_id)
