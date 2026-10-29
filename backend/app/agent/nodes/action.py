"""execute_action 节点：把 Agent 的处理方案落到业务系统（v2.md §38 / §70）。

三件事，按顺序：
    1. 回填工单分类（Agent 判断出的 issue_type → tickets.type，方便客服检索）；
    2. 执行业务动作（目前是 apply_refund）—— **必须走工具 → Service**，
       由 RefundService 再校验一遍；人工批准过的会把 approved_by 带上；
    3. 把结论写入工单对话（sender_type=AGENT），并把工单推进到 RESOLVED。

如果人工驳回了（human_approved=False），这里直接跳过业务动作，工单留给客服人工处理。
"""

from app.agent.events import emit_event
from app.agent.state import AgentState
from app.core.logging import get_logger
from app.models.agent_event import AgentEventType
from app.models.side_effect import SideEffectKind
from app.models.ticket import TicketStatus, TicketType
from app.models.ticket_message import SenderType
from app.services import ticket_service
from app.services.side_effect_service import SideEffectGuard
from app.tools import ACTION_TOOLS

logger = get_logger(__name__)

# AgentDecision.issue_type → tickets.type
# 模型输出已被 Literal 约束为英文枚举，这里仍保留中文兜底映射：
# 万一换模型后返回了中文/近义词，也不会把工单分类丢掉。
TYPE_MAPPING = {
    "ORDER": TicketType.ORDER,
    "LOGISTICS": TicketType.LOGISTICS,
    "REFUND": TicketType.REFUND,
    "AFTER_SALE": TicketType.AFTER_SALE,
    "OTHER": TicketType.OTHER,
    # 兜底
    "订单": TicketType.ORDER,
    "订单问题": TicketType.ORDER,
    "物流": TicketType.LOGISTICS,
    "物流问题": TicketType.LOGISTICS,
    "退款": TicketType.REFUND,
    "退款问题": TicketType.REFUND,
    "售后": TicketType.AFTER_SALE,
    "售后问题": TicketType.AFTER_SALE,
    "其他": TicketType.OTHER,
}


async def execute_action(state: AgentState) -> dict:
    """执行处理方案并更新工单。"""
    # 同步 Service/工具在独立线程运行，租约心跳和事件循环不受行锁等待影响。
    import asyncio
    return await asyncio.to_thread(_execute_action, state)


def _execute_action(state: AgentState) -> dict:
    ticket_id = state["ticket_id"]
    agent_run_id = state.get("agent_run_id")
    result = state.get("result") or {}
    decision = state.get("decision") or {}
    human_approved = state.get("human_approved")
    review_round = state.get("human_review_round", 0)
    key = f"run:{agent_run_id}:ticket:{ticket_id}" if agent_run_id is not None else None

    # ---- 人工驳回：不执行任何业务动作 ----
    if human_approved is False:
        return _reject_action(state, f"{key}:rejected:{review_round}" if key else None)

    action = state.get("business_action")
    valid_answer = (result.get("resolution_kind") == "ANSWER"
                    and decision.get("request_kind") == "CONSULTATION" and not action)
    valid_action = result.get("resolution_kind") == "ACTION" and action in ("apply_refund", "cancel_order")
    if not str(result.get("solution") or "").strip() or not (valid_answer or valid_action):
        return {"status": "WAITING_HUMAN", "need_human": True,
                "action_result": {"executed": False, "error": "INCOMPLETE_PLAN",
                                  "message": "方案不完整或需要补充信息，不能自动结单"}}

    # ---- ① 回填工单分类 ----
    ticket_type = TYPE_MAPPING.get(decision.get("issue_type") or result.get("issue_type") or "")
    if ticket_type:
        ACTION_TOOLS["update_ticket"].invoke({"ticket_id": ticket_id, "type": ticket_type})

    # ---- ② 执行业务动作 ----
    action_result: dict = {"executed": False, "reason": "无需业务动作"}

    if action == "apply_refund":
        emit_event(
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
            event_type=AgentEventType.EXECUTING_REFUND,
            message="正在执行退款（RefundService 正在做最终校验）",
            dedup_key=f"{key}:refund:{review_round}" if key else None,
        )
        action_result = _refund_with_idempotency_guard(
            order_id=state.get("order_id"),
            user_id=state["user_id"],
            reason=result.get("solution") or result.get("summary") or "售后申请",
            approved_by=state.get("human_operator_id") if human_approved is True else None,
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
        )
        logger.info("退款动作结果：ticket_id=%s %s", ticket_id, action_result)
    elif action == "cancel_order":
        action_result = _cancel_with_idempotency_guard(state)
    elif action:
        action_result = {"success": False, "executed": False,
                         "error": "UNSUPPORTED_ACTION", "message": f"不支持的业务动作：{action}"}

    # ---- ③ 把结论写进工单对话 + 推进状态 ----
    solution = result.get("solution") or result.get("summary") or "已处理完成"
    if action and not action_result.get("success", True):
        # 业务规则拒绝（例如状态不符）→ 转人工，别把用户晾着
        status = "WAITING_HUMAN"
        ticket_service.mark_waiting_human(ticket_id)
        message = f"业务动作未能自动执行（{action_result.get('error')}），已转人工处理"
        emit_event(
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
            event_type=AgentEventType.WAITING_HUMAN,
            message=message,
            dedup_key=f"{key}:action-refused:{review_round}" if key else None,
        )
    elif action and not action_result.get("executed"):
        # 需要人工审核但当前没有人批准（理论上 human_review 已经拦过，这里是兜底）
        status = "WAITING_HUMAN"
        ticket_service.mark_waiting_human(ticket_id)
    else:
        status = "COMPLETED"
        ticket = ticket_service.get_ticket_raw(ticket_id)
        if ticket["status"] != TicketStatus.CLOSED:
            ticket_service.resolve_ticket(
                ticket_id=ticket_id,
                solution=solution,
                operator_id=state.get("human_operator_id"),
                agent_run_id=agent_run_id,
                agent_reply=True,
            )
        emit_event(
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
            event_type=AgentEventType.COMPLETED,
            message="Agent 处理完成",
            dedup_key=f"{key}:completed" if key else None,
        )

    if status == "WAITING_HUMAN":
        reason = action_result.get("reason") or action_result.get("message") or action_result.get("error")
        prefix = "退款" if action == "apply_refund" else "业务动作"
        _append_agent_message(ticket_id, f"{prefix}尚未完成，等待人工处理：{reason or '请核实处理状态'}",
                              dedup_key=f"{key}:waiting:{review_round + 1}" if key else None)
        return {"status": status, "action_result": action_result, "need_human": True}
    return {"status": status, "action_result": action_result}


def _cancel_with_idempotency_guard(state: AgentState) -> dict:
    order_id = state.get("order_id")
    if order_id is None:
        return {"success": False, "executed": False, "error": "ORDER_REQUIRED",
                "message": "取消订单需要关联订单"}
    guard = SideEffectGuard(kind=SideEffectKind.ORDER_CANCEL,
                            agent_run_id=state.get("agent_run_id"),
                            target=f"order:{order_id}", ticket_id=state["ticket_id"])
    existing = guard.claim()
    if existing is not None:
        previous = existing.get("result") or {}
        if previous.get("success") and previous.get("executed"):
            return {**previous, "duplicate": True}
        from app.services.order_service import get_order_detail
        current = get_order_detail(order_id, user_id=state["user_id"])
        if current["fulfillment_status"] == "CANCELLED":
            return {"success": True, "executed": True, "duplicate": True, "order": current}
        return {"success": False, "executed": False, "error": "CANCEL_RESULT_UNKNOWN",
                "message": "已有取消认领但结果未知，请人工核实订单状态"}
    try:
        output = ACTION_TOOLS["cancel_order"].invoke({"order_id": order_id, "user_id": state["user_id"]})
        if output.get("success") and output.get("executed"):
            guard.complete(output)
        else:
            guard.release()
        return output
    except Exception:
        guard.release()
        raise


def _refund_with_idempotency_guard(
    *,
    order_id: int | None,
    user_id: int,
    reason: str,
    approved_by: int | None,
    agent_run_id: int | None,
    ticket_id: int,
) -> dict:
    """带幂等键的退款调用（Agent → Tool → Service → Repository → MySQL）。

    为什么在"业务规则之外"还要一层幂等键：
        RefundService 已经会拦重复退款（订单已退款 / 有进行中的退款），
        但那套判断依赖"每次都读得到最新状态"。真正兜底的是数据库唯一约束：
        同一次 Agent 执行对同一个订单的退款，只会有一条 side_effect_claims 记录。

        于是以下三种情况都不会重复动钱：
            · 同一次执行内的重试（含从断点续跑）
            · 恢复执行后再次走到这里
            · 并发请求同时打到同一个副作用

    注意粒度：键里带 agent_run_id，所以"用户重新发起一次处理"是新的一次意图，
    会走新的键 —— 该不该退仍然由 RefundService 的业务规则判断。
    """
    if order_id is None:
        return {"success": False, "executed": False, "error": "ORDER_REQUIRED",
                "message": "工单未关联订单，请人工核实后处理退款"}

    guard = SideEffectGuard(
        kind=SideEffectKind.REFUND,
        agent_run_id=agent_run_id,
        target=f"order:{order_id}",
        ticket_id=ticket_id,
    )
    existing = guard.claim()
    if existing is not None:
        previous = existing.get("result") or {}
        if not (previous.get("success") and previous.get("executed")):
            # 业务事务已提交、幂等快照尚未保存就崩溃：先核实资金流水再决定。
            from app.core.database import session_scope
            from app.repositories import refund_repository, order_repository
            with session_scope() as db:
                current = order_repository.get_by_id(db, order_id)
                refund = refund_repository.find_success_by_order(db, order_id)
                if (current and current["user_id"] == user_id
                        and current["payment_status"] == "REFUNDED" and refund):
                    return {"success": True, "executed": True, "duplicate": True,
                            "refund": refund, "order": current}
            # 认领成功不等于退款成功；结果未知时不重复动钱，也不假装结单。
            return {"success": False, "executed": False, "need_human": True,
                    "error": "REFUND_RESULT_UNKNOWN",
                    "message": "已有退款认领，但缺少成功结果，请人工核实退款流水"}
        logger.info("退款幂等键命中，复用已完成结果：run=%s order=%s", agent_run_id, order_id)
        return {
            "success": True,
            "executed": True,
            "duplicate": True,
            "reason": "同一次 Agent 执行内已经执行过这笔退款，已跳过重复操作",
            "refund": previous.get("refund"),
            "order": previous.get("order"),
        }

    try:
        output = ACTION_TOOLS["apply_refund"].invoke(
            {
                "order_id": order_id,
                "user_id": user_id,
                "amount": None,               # None = 订单全额
                "reason": reason,
                "approved_by": approved_by,   # 人工批准过就带上
            }
        )
    except Exception:
        # 真实操作炸了 → 放开认领，别把后续重试挡在门外
        guard.release()
        raise

    if output.get("success"):
        # 只有"确实做成了"才留记录；需要人工审核（executed=False）不算做过
        guard.complete(output if output.get("executed") else None)
        if not output.get("executed"):
            guard.release()
    else:
        # 业务规则拒绝（例如订单不存在）→ 放开，允许用户重新发起时再次尝试
        guard.release()
    return output


def _append_agent_message(ticket_id: int, content: str, *, dedup_key: str | None = None) -> None:
    """把 Agent 的回复写入工单消息（用户在前端就能看到 Agent 说了什么）。"""
    try:
        ticket_service.add_message(
            ticket_id=ticket_id,
            content=content,
            sender_type=SenderType.AGENT,
            sender_id=None,
            dedup_key=dedup_key,
        )
    except Exception as exc:  # noqa: BLE001 —— 写消息失败不能影响主流程
        logger.warning("写入 Agent 消息失败：%s", exc)
        if dedup_key is not None:
            raise


def _reject_action(state: AgentState, dedup_key: str | None) -> dict:
    """驳回售后与交接消息同事务提交，崩溃恢复时可以安全重放。"""
    from app.core.database import session_scope
    from app.models.agent_run import AgentRun
    from app.repositories import after_sale_repository, order_repository, ticket_repository
    from app.services import refund_service
    from app.services.agent_task_service import order_review_snapshot

    comment = state.get("human_comment") or "客服驳回了 Agent 的处理方案"
    rejected = {"status": "REJECTED", "action_result": {"executed": False, "reason": comment}}
    with session_scope() as db:
        if dedup_key and ticket_repository.has_message_key(db, dedup_key):
            return rejected
        if state.get("business_action") == "apply_refund" and state.get("order_id"):
            order_repository.get_model_for_update(db, state["order_id"])
            run = db.get(AgentRun, state["agent_run_id"])
            expected = (run.review_payload or {}).get("order_snapshot")
            if expected != order_review_snapshot(db, state["order_id"]):
                return {"status": "WAITING_HUMAN", "need_human": True,
                        "action_result": {"executed": False,
                            "message": "订单或售后状态已变化，原驳回决定不能作用于新申请"}}
            sale = after_sale_repository.find_active_by_order(db, state["order_id"])
            if sale and sale["type"] == "REFUND":
                refund_service.reject_refund(order_id=state["order_id"],
                    operator_id=state["human_operator_id"], reason=comment)
        ticket_repository.add_message(db, ticket_id=state["ticket_id"], sender_type=SenderType.AGENT,
            content=f"【处理方案未通过人工审核】{comment}", dedup_key=dedup_key)
    return rejected
