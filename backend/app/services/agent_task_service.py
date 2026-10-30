"""持久化 Agent 任务：提交、领取、审批、续租与收尾。

API 不运行图。Worker 是唯一图执行者。锁顺序统一为工单 → Run → 订单。
"""
from datetime import timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import AgentExecutionError, PermissionDeniedError, TicketNotFoundError
from app.core.ids import gen_thread_id
from app.core.time import utcnow
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.agent_event import AgentEventType
from app.models.ticket import Ticket, TicketStatus, AgentStatus, HandlingMode
from app.models.ticket_message import SenderType
from app.repositories import agent_run_repository, ticket_repository


def conflict(message: str):
    """以统一业务码拒绝已经过期或不允许的任务状态操作。"""
    raise AgentExecutionError(message, code="AGENT_STATE_CONFLICT", http_status=409)


def lock_ticket_run(db, ticket_id: int):
    """按工单、当前任务的固定顺序加行锁，后续校验与更新共用事务。"""
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if ticket is None:
        raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
    run = (db.scalar(select(AgentRun).where(AgentRun.id == ticket.agent_run_id).with_for_update())
           if ticket.agent_run_id else None)
    return ticket, run


def submit_agent_run(*, ticket_id: int, user_id: int) -> dict:
    """锁工单后复用活动任务或创建持久化排队任务，不在请求中执行图。"""
    from app.agent.version import execution_spec
    with session_scope() as db:
        ticket, run = lock_ticket_run(db, ticket_id)
        if ticket.user_id != user_id:
            raise PermissionDeniedError("无权处理他人的工单")
        if ticket.handling_mode != HandlingMode.AGENT:
            conflict("工单已请求人工处理，须由负责客服明确交回后才能启动 Agent")
        reused = run is not None and run.status in AgentRunStatus.ACTIVE
        if not reused:
            if ticket.status in (TicketStatus.CLOSED, TicketStatus.RESOLVED):
                conflict("请先重新打开工单，再启动 Agent")
            run = AgentRun(ticket_id=ticket.id, user_id=user_id,
                           thread_id=gen_thread_id(ticket_id), status=AgentRunStatus.PENDING,
                           flow_version=execution_spec()["version"], execution_spec=execution_spec())
            db.add(run)
            db.flush()
            ticket.agent_run_id = run.id
            ticket.agent_status = AgentStatus.RUNNING  # 兼容展示；不是领取判据
            ticket.status = TicketStatus.PROCESSING
            agent_run_repository.add_event(db, agent_run_id=run.id,
                event_type=AgentEventType.STARTED, message="任务已提交，等待 worker 领取")
        db.flush()
        return {"agent_run": run.to_dict(), "status": run.status, "reused": reused,
                "need_human": run.status == AgentRunStatus.WAITING_HUMAN,
                "message": "返回已有任务" if reused else "任务已排队"}


def claim_agent_run(worker_id: str) -> dict | None:
    """查候选后加锁复查，领取排队或租约过期任务并递增执行代次。"""
    now = utcnow()
    with session_scope() as db:
        # 只选候选 ID；拿到工单与 Run 的锁后再复查，不依赖这次普通读的结果。
        candidate = db.execute(select(AgentRun.id, AgentRun.ticket_id).where(or_(
            (AgentRun.status == AgentRunStatus.PENDING) & or_(
                AgentRun.next_attempt_at.is_(None), AgentRun.next_attempt_at <= now),
            (AgentRun.status == AgentRunStatus.RUNNING) & or_(
                AgentRun.lease_until.is_(None), AgentRun.lease_until <= now),
        )).order_by(AgentRun.id).limit(1)).first()
        if candidate is None:
            return None
        ticket, run = lock_ticket_run(db, candidate.ticket_id)
        if run is None or run.id != candidate.id:
            # 历史遗留的孤立执行不能阻塞队首，也不能复活。
            orphan = db.get(AgentRun, candidate.id)
            if orphan is not None and orphan.status in ("PENDING", "RUNNING"):
                orphan.status = AgentRunStatus.FAILED
                orphan.error = "工单已不再关联此任务，请人工核实"
            return None
        if run.status == AgentRunStatus.PENDING:
            if run.next_attempt_at and run.next_attempt_at > now:
                return None
        elif run.status == AgentRunStatus.RUNNING:
            if run.lease_until and run.lease_until > now:
                return None
        else:
            return None
        recovered = run.status == AgentRunStatus.RUNNING
        run.status = AgentRunStatus.RUNNING
        run.worker_id = worker_id
        run.execution_epoch += 1
        run.lease_until = now + timedelta(seconds=settings.agent_lease_seconds)
        run.alive_at = now
        run.started_at = run.started_at or now
        run.finished_at = None
        run.next_attempt_at = None
        ticket.agent_status = AgentStatus.RUNNING
        # 动作已提交但任务尚未收尾就崩溃时，工单可能已经解决。
        # 恢复只补执行进度，不能覆盖已提交的解决状态；用户主动重开则仍保持处理中。
        if ticket.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            ticket.status = TicketStatus.PROCESSING
        agent_run_repository.add_event(db, agent_run_id=run.id, event_type=AgentEventType.STARTED,
            message="租约过期，恢复未完成任务" if recovered else "Worker 已领取任务")
        db.flush()
        return run.to_dict()


def renew_execution_lease(claim) -> None:
    # 由独立心跳线程调用，其 ContextVar 不携带业务事务。
    """先确认执行凭证有效，再更新活跃时间并延长租约。"""
    from app.core.execution import assert_execution
    with session_scope() as db:
        assert_execution(db, claim, allow_handoff=True)
        run = db.get(AgentRun, claim.run_id)
        run.alive_at = utcnow()
        run.lease_until = run.alive_at + timedelta(seconds=settings.agent_lease_seconds)


def order_review_snapshot(db, order_id: int | None) -> dict | None:
    """读取订单与售后审核快照，用于拒绝作用于旧业务状态的审批。"""
    if order_id is None:
        return None
    from app.repositories import order_repository, after_sale_repository
    order = order_repository.get_model_for_update(db, order_id)
    if order is None:
        conflict("关联订单不存在")
    sale = after_sale_repository.find_active_by_order(db, order_id)
    return {"order_id": order.id, "payment_status": order.payment_status,
            "fulfillment_status": order.fulfillment_status, "total_amount": str(order.total_amount),
            "after_sale": None if sale is None else {
                "id": sale["id"], "status": sale["status"], "type": sale["type"],
                "amount": str(sale["amount"]), "reason": sale.get("reason")}}


def save_review(state: dict, payload: dict, review_round: int) -> dict:
    """中断前持久化方案；节点重放时同一轮不覆盖原快照和审批决定。"""
    with session_scope() as db:
        ticket, run = lock_ticket_run(db, state["ticket_id"])
        if run is None or run.id != state.get("agent_run_id"):
            conflict("审核任务已失效")
        if run.review_round < review_round or not run.review_payload:
            reset_review_deadline(run)
            run.review_round = review_round
            run.review_payload = jsonable_encoder({**payload, "review_round": review_round,
                "expires_at": run.review_deadline_at,
                "order_snapshot": order_review_snapshot(db, ticket.order_id)})
            run.approval_decision = None
        run.result = jsonable_encoder(state.get("result"))
        run.decision = jsonable_encoder(state.get("decision"))
        return run.review_payload


def submit_approval(*, ticket_id: int, operator_id: int, approved: bool,
                    expected_run_id: int, review_round: int, comment: str | None = None) -> dict:
    """校验任务、审核轮次和业务快照，持久化本轮决定供 worker 恢复。"""
    with session_scope() as db:
        ticket, run = lock_ticket_run(db, ticket_id)
        from app.services.refund_service import _validate_operator
        _validate_operator(db, operator_id)
        if run is None or run.id != expected_run_id or run.review_round != review_round:
            conflict("审批方案已变化，请刷新后重新审核")
        if not run.review_payload:
            conflict("旧任务缺少可验证的审批方案，请人工接管")
        if ticket.handling_mode != HandlingMode.AGENT:
            conflict("工单已转人工，不能恢复旧审批方案")
        from app.agent.version import compatible
        if not compatible(run.to_dict()):
            conflict("流程、Prompt 或模型版本已变化，禁止恢复旧方案；请使用原版本处理或人工接管")
        if run.approval_decision is not None:
            if run.approval_decision["approved"] != approved:
                conflict("该轮审批已提交，不能反转决定")
            return run.to_dict()
        if run.status != AgentRunStatus.WAITING_HUMAN:
            conflict("当前任务不处于等待审批状态")
        if run.review_deadline_at is None or run.review_deadline_at <= utcnow():
            conflict("审批方案已过期，不能继续批准或驳回；请刷新后由客服接管")
        if approved:
            result = run.result or {}
            decision = run.decision or {}
            valid_answer = (result.get("resolution_kind") == "ANSWER"
                            and decision.get("request_kind") == "CONSULTATION")
            valid_action = (result.get("resolution_kind") == "ACTION"
                            and result.get("business_action") in ("apply_refund", "cancel_order"))
            if not (valid_answer or valid_action) or not str(result.get("solution") or "").strip():
                conflict("该方案缺少可执行动作或有效答复，请补充信息或人工接管")
        latest = order_review_snapshot(db, ticket.order_id)
        changed = latest != run.review_payload.get("order_snapshot")
        if changed:
            # 旧批准和旧驳回都不能作用于已变化的售后；先保存新版本再返回冲突。
            reset_review_deadline(run)
            run.review_payload = jsonable_encoder({**run.review_payload, "order_snapshot": latest,
                "reason": "订单或售后状态已变化，请核对后重新审核",
                "review_round": run.review_round + 1, "expires_at": run.review_deadline_at})
            run.review_round += 1
        if not changed:
            run.approval_decision = {"approved": approved, "operator_id": operator_id,
                                     "comment": comment, "review_round": review_round}
            run.status = AgentRunStatus.PENDING
            run.retry_count = 0
            run.next_attempt_at = None
            ticket.agent_status = AgentStatus.RUNNING
            ticket.status = TicketStatus.PROCESSING
            agent_run_repository.add_event(db, agent_run_id=run.id,
                event_type=AgentEventType.HUMAN_APPROVED if approved else AgentEventType.HUMAN_REJECTED,
                message="审批决定已提交，等待 worker 恢复执行",
                dedup_key=f"run:{run.id}:submitted-review:{review_round}")
        db.flush()
        result = run.to_dict()
    result["review_changed"] = changed
    return result


def reset_review_deadline(run) -> None:
    """新审核轮次重新计时；中断重放和重复提交不能延长同一轮的期限。"""
    run.review_requested_at = utcnow()
    run.review_deadline_at = run.review_requested_at + timedelta(seconds=settings.agent_review_timeout_seconds)
    run.review_reminded_at = None


def sweep_reviews(limit: int = 100) -> dict:
    """扫描审核提醒与到期，按工单→任务加锁，与审批或接管互斥。"""
    now = utcnow()
    remind_before = now - timedelta(seconds=settings.agent_review_reminder_seconds)
    with session_scope() as db:
        candidates = db.execute(select(AgentRun.ticket_id, AgentRun.id).join(
            Ticket, Ticket.agent_run_id == AgentRun.id).where(
            AgentRun.status == AgentRunStatus.WAITING_HUMAN,
            or_(AgentRun.review_deadline_at.is_(None), AgentRun.review_deadline_at <= now,
                (AgentRun.review_reminded_at.is_(None)) & (AgentRun.review_requested_at <= remind_before)),
        ).order_by(AgentRun.id).limit(limit)).all()
    counts = {"reminded": 0, "expired": 0}
    for ticket_id, run_id in candidates:
        with session_scope() as db:
            ticket, run = lock_ticket_run(db, ticket_id)
            if run is None or run.id != run_id or run.status != AgentRunStatus.WAITING_HUMAN:
                continue
            key = f"run:{run.id}:review:{run.review_round}"
            now = utcnow()
            if run.review_deadline_at is None or run.review_deadline_at <= now:
                # 只结束自动任务并交人工，不修改售后申请、不批准退款、不删除 Checkpoint。
                run.status = AgentRunStatus.FAILED
                run.error = "REVIEW_EXPIRED：审批超过期限或缺少有效期限，请由客服核实处理"
                run.finished_at = now
                run.worker_id = None
                run.lease_until = None
                ticket.agent_run_id = None
                ticket.agent_status = AgentStatus.IDLE
                ticket.handling_mode = HandlingMode.HUMAN
                ticket.status = TicketStatus.PROCESSING if ticket.assignee_id else TicketStatus.WAITING_HUMAN
                message = "审批方案已过期，Agent 已停止，工单转客服跟进；系统不会因超时自动退款，请核实业务状态"
                ticket_repository.add_message(db, ticket_id=ticket.id, sender_type=SenderType.SYSTEM,
                    content=message, dedup_key=f"{key}:expired")
                agent_run_repository.add_event(db, agent_run_id=run.id, event_type=AgentEventType.REVIEW_EXPIRED,
                    message=message, dedup_key=f"{key}:expired-event")
                counts["expired"] += 1
            elif run.review_reminded_at is None and run.review_requested_at and run.review_requested_at <= remind_before:
                run.review_reminded_at = now
                message = "审批待办提醒：请客服核对方案并在截止时间前处理"
                ticket_repository.add_message(db, ticket_id=ticket.id, sender_type=SenderType.SYSTEM,
                    content=message, dedup_key=f"{key}:reminder")
                agent_run_repository.add_event(db, agent_run_id=run.id, event_type=AgentEventType.REVIEW_REMINDER,
                    message=message, dedup_key=f"{key}:reminder-event")
                counts["reminded"] += 1
    return counts


def finish_agent_run(run_id: int, state: dict) -> None:
    """在同一事务中更新任务和工单投影；执行凭证由 session_scope 校验。"""
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        ticket = db.get(Ticket, run.ticket_id)
        waiting = bool(state.get("__interrupt__"))
        status = AgentRunStatus.WAITING_HUMAN if waiting else state.get("status", "FAILED")
        if status not in (*AgentRunStatus.TERMINAL, AgentRunStatus.WAITING_HUMAN):
            status = AgentRunStatus.FAILED
        run.status = status
        run.result = jsonable_encoder(state.get("result"))
        run.decision = jsonable_encoder(state.get("decision"))
        run.used_rag = bool(state.get("used_rag"))
        run.used_tool = bool(state.get("used_tool"))
        run.need_human = waiting
        run.error = None
        run.finished_at = None if waiting else utcnow()
        run.current_node = None
        run.worker_id = None
        run.lease_until = None
        ticket.agent_status = AgentStatus.WAITING_HUMAN if waiting else AgentStatus.IDLE
        if waiting:
            ticket.status = TicketStatus.WAITING_HUMAN
        else:
            ticket.agent_run_id = None
            if status == AgentRunStatus.REJECTED:
                ticket.handling_mode = HandlingMode.HUMAN
                ticket.assignee_id = (run.approval_decision or {}).get("operator_id") or ticket.assignee_id
                ticket.status = TicketStatus.PROCESSING
                ticket_repository.add_message(db, ticket_id=ticket.id, sender_type=SenderType.SYSTEM,
                    content="人工驳回了处理方案，工单转人工跟进",
                    dedup_key=f"run:{run.id}:rejected-handoff:{run.review_round}")
            elif status == AgentRunStatus.FAILED:
                ticket.status = TicketStatus.FAILED


def fail_agent_run(run_id: int, error: Exception) -> None:
    """可恢复故障有限退避重试，最终失败结束任务并交人工核实。"""
    from app.services.agent_service import is_retryable_error
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        ticket = db.get(Ticket, run.ticket_id)
        retry = is_retryable_error(error) and run.retry_count < settings.agent_max_retries
        run.error = str(error)[:1000]
        run.worker_id = None
        run.lease_until = None
        run.current_node = None
        if retry:
            run.retry_count += 1
            run.status = AgentRunStatus.PENDING
            run.next_attempt_at = utcnow() + timedelta(seconds=min(60, 2 ** run.retry_count))
        else:
            run.status = AgentRunStatus.FAILED
            run.finished_at = utcnow()
            ticket.status = TicketStatus.FAILED
            ticket.agent_status = AgentStatus.IDLE
            ticket.agent_run_id = None
            from app.services.handoff_service import request_locked
            request_locked(db, ticket.id, "Agent 自动处理失败且无法继续，请人工核实：" + str(error)[:300])
        agent_run_repository.add_event(db, agent_run_id=run.id,
            event_type=AgentEventType.RETRYING if retry else AgentEventType.FAILED,
            message="执行失败，已安排稍后重试" if retry else "执行失败，请由客服核实处理")


def stream_snapshot(ticket_id: int, after_id: int) -> tuple[list[dict], dict | None]:
    """按事件主键游标读取工单最新任务的持久化事件和任务状态。"""
    from app.models.agent_event import AgentRunEvent
    with session_scope() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.ticket_id == ticket_id)
                        .order_by(AgentRun.id.desc()).limit(1))
        if run is None:
            return [], None
        events = db.scalars(select(AgentRunEvent).where(
            AgentRunEvent.agent_run_id == run.id, AgentRunEvent.id > after_id
        ).order_by(AgentRunEvent.id).limit(200)).all()
        return [{"type": "event", "seq": e.id, "event_id": e.id,
                 "agent_run_id": run.id, "ticket_id": ticket_id,
                 "event_type": e.event_type, "message": e.message,
                 "at": e.created_at.isoformat() + "Z"} for e in events], run.to_dict()
