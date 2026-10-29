"""Agent 异常边界：真实内存图，业务写操作隔离，不调用外部模型。"""

from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import nodes
from app.agent.graph import build_graph
from app.core.exceptions import AgentExecutionError
from app.core.time import utcnow
from app.services import agent_service, ticket_service


@pytest.fixture
def boundary_graph(monkeypatch):
    async def analyze(state):
        return {"decision": {"issue_type": "REFUND"}}

    async def result(state):
        return {"result": {"solution": "退款处理方案"}, "need_human": False,
                "business_action": "apply_refund"}

    monkeypatch.setattr(nodes.analyze, "analyze_ticket", analyze)
    monkeypatch.setattr(nodes.result, "generate_result", result)
    monkeypatch.setitem(nodes.action.ACTION_TOOLS, "update_ticket", MagicMock())
    monkeypatch.setattr(nodes.action, "emit_event", MagicMock())
    monkeypatch.setattr(nodes.human, "emit_event", MagicMock())
    monkeypatch.setattr(nodes.action, "_append_agent_message", MagicMock())
    monkeypatch.setattr(ticket_service, "mark_waiting_human", MagicMock())
    monkeypatch.setattr(ticket_service, "mark_processing", MagicMock())
    monkeypatch.setattr(ticket_service, "add_message", MagicMock())
    monkeypatch.setattr(ticket_service, "get_ticket_raw", MagicMock(return_value={"status": "PROCESSING"}))
    monkeypatch.setattr(ticket_service, "resolve_ticket", MagicMock())
    refund = MagicMock(return_value={"success": True, "executed": False,
                                  "need_human": True, "reason": "金额超限"})
    monkeypatch.setattr(nodes.action, "_refund_with_idempotency_guard", refund)
    graph = build_graph(InMemorySaver())
    monkeypatch.setattr(agent_service, "get_graph", lambda: graph)
    monkeypatch.setattr(ticket_service, "get_active_run", lambda _: {
        "id": 1, "thread_id": "boundary", "status": "WAITING_HUMAN"})
    monkeypatch.setattr(ticket_service, "acquire_agent_execution_for_resume", lambda *a, **kw: True)
    monkeypatch.setattr(agent_service, "session_scope", lambda: nullcontext(None))
    monkeypatch.setattr(agent_service.agent_run_repository, "mark_finished", MagicMock())
    monkeypatch.setattr(agent_service, "get_run", lambda _: {})
    monkeypatch.setattr(agent_service, "emit_done", MagicMock())
    settle = MagicMock(return_value=True)
    monkeypatch.setattr(ticket_service, "settle_agent_execution", settle)
    return graph, refund, settle


async def start(graph):
    return await graph.ainvoke({"ticket_id": 1, "user_id": 1, "order_id": 1},
                              {"configurable": {"thread_id": "boundary"}})


async def test_business_fallback_has_interrupt_and_approval_executes(boundary_graph):
    graph, refund, settle = boundary_graph
    waiting = await start(graph)
    assert waiting["need_human"] is True
    assert "__interrupt__" in waiting
    refund.return_value = {"success": True, "executed": True}
    outcome = await agent_service.resume_with_decision(ticket_id=1, operator_id=9, approved=True)
    assert outcome["status"] == "COMPLETED"
    assert refund.call_count == 2
    assert refund.call_args.kwargs["approved_by"] == 9
    assert settle.call_args.kwargs["idle"] is True


async def test_failed_action_after_approval_waits_again(boundary_graph):
    graph, refund, settle = boundary_graph
    await start(graph)
    refund.return_value = {"success": False, "executed": False, "error": "REFUND_RESULT_UNKNOWN"}
    outcome = await agent_service.resume_with_decision(ticket_id=1, operator_id=9, approved=True)
    assert outcome["status"] == "WAITING_HUMAN"
    snapshot = await graph.aget_state({"configurable": {"thread_id": "boundary"}})
    assert any(task.interrupts for task in snapshot.tasks)
    assert settle.call_args.kwargs["idle"] is False
    assert not ticket_service.resolve_ticket.called


async def test_reject_business_fallback_does_not_refund(boundary_graph):
    graph, refund, _ = boundary_graph
    await start(graph)
    outcome = await agent_service.resume_with_decision(ticket_id=1, operator_id=9, approved=False)
    assert outcome["status"] == "REJECTED"
    assert refund.call_count == 1


async def test_completed_checkpoint_cannot_be_approved(boundary_graph):
    graph, refund, _ = boundary_graph
    refund.return_value = {"success": True, "executed": True}
    await start(graph)
    with pytest.raises(AgentExecutionError, match="没有可恢复"):
        await agent_service.resume_with_decision(ticket_id=1, operator_id=9, approved=True)
    assert refund.call_count == 1


@pytest.mark.parametrize("previous", [None, {}, {"success": True, "executed": False}])
def test_incomplete_claim_is_not_refund_success(monkeypatch, previous):
    guard = MagicMock()
    guard.claim.return_value = {"result": previous}
    monkeypatch.setattr(nodes.action, "SideEffectGuard", lambda **kw: guard)
    tool = MagicMock()
    monkeypatch.setitem(nodes.action.ACTION_TOOLS, "apply_refund", tool)
    outcome = nodes.action._refund_with_idempotency_guard(
        order_id=1, user_id=1, reason="test", approved_by=None, agent_run_id=1, ticket_id=1)
    assert outcome["success"] is False
    assert outcome["executed"] is False
    assert outcome["error"] == "REFUND_RESULT_UNKNOWN"
    tool.invoke.assert_not_called()
    guard.release.assert_not_called()


def test_completed_claim_replays_confirmed_result(monkeypatch):
    guard = MagicMock()
    guard.claim.return_value = {"result": {"success": True, "executed": True, "refund": {"id": 7}}}
    monkeypatch.setattr(nodes.action, "SideEffectGuard", lambda **kw: guard)
    monkeypatch.setattr(nodes.action, "emit_event", MagicMock())
    tool = MagicMock()
    monkeypatch.setitem(nodes.action.ACTION_TOOLS, "apply_refund", tool)
    outcome = nodes.action._refund_with_idempotency_guard(
        order_id=1, user_id=1, reason="test", approved_by=None, agent_run_id=1, ticket_id=1)
    assert outcome["executed"] is True
    assert outcome["refund"]["id"] == 7
    tool.invoke.assert_not_called()


def test_settlement_uses_original_run(monkeypatch):
    settle = MagicMock()
    monkeypatch.setattr(ticket_service, "settle_agent_execution", settle)
    read = MagicMock(return_value={"agent_run_id": 999})
    monkeypatch.setattr(ticket_service, "get_agent_state", read)
    agent_service._settle_after_execution(1, agent_run_id=7, final_status="COMPLETED")
    assert settle.call_args.kwargs["agent_run_id"] == 7
    read.assert_not_called()


async def test_zero_retries_means_one_attempt(monkeypatch):
    monkeypatch.setattr(agent_service.settings, "agent_max_retries", 0)
    invoke = AsyncMock(side_effect=TimeoutError("timeout"))
    monkeypatch.setattr(agent_service, "_invoke_graph", invoke)
    for name in ("_mark_run_failed", "emit_error", "emit_done"):
        monkeypatch.setattr(agent_service, name, MagicMock())
    with pytest.raises(AgentExecutionError):
        await agent_service._run_with_retries(
            ticket={}, ticket_id=1, content=None, operator_id=None,
            run_factory=lambda: {"id": 1, "thread_id": "zero"}, on_new_run=lambda _: None)
    assert invoke.await_count == 1


@pytest.mark.parametrize("run_id", [None, 7])
def test_fresh_unbound_or_retrying_execution_is_not_reaped(monkeypatch, run_id):
    monkeypatch.setattr(ticket_service, "session_scope", lambda: nullcontext(None))
    monkeypatch.setattr(ticket_service.ticket_repository, "get_agent_state",
                        lambda *a: {"agent_status": "RUNNING", "agent_run_id": run_id})
    monkeypatch.setattr(ticket_service.ticket_repository, "get_by_id",
                        lambda *a: {"updated_at": utcnow()})
    monkeypatch.setattr(ticket_service.agent_run_repository, "get_run",
                        lambda *a: {"id": 7, "status": "FAILED", "alive_at": utcnow() - timedelta(seconds=1)})
    settle = MagicMock()
    monkeypatch.setattr(ticket_service.ticket_repository, "settle_agent_execution", settle)
    assert ticket_service.reap_stale_execution(1, stale_seconds=1800, still_expected_run_id=None) is None
    settle.assert_not_called()
