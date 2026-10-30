"""慢事件事务或审批保存不能阻塞同一 worker 中其他工单的调度。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.agent import events
from app.agent.nodes import analyze, human, result, retrieve, tools
from app.agent.schemas import AgentDecision, AgentResult
from app.core.execution import ExecutionClaim, ExecutionLost, execution_claim


@pytest.mark.asyncio
@pytest.mark.parametrize("node_name", ["analyze", "retrieve", "tools", "result", "human", "review_save"])
async def test_slow_node_io_yields_to_other_work(monkeypatch, node_name):
    """模拟行锁等待：另一协程必须能运行并解除等待，且执行凭证传到 I/O 线程。"""
    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    observed = []
    claim = ExecutionClaim(1, 2, 3, "test-worker")

    def slow_io():
        observed.append((threading.get_ident(), execution_claim.get()))
        entered.set()
        assert release.wait(2), "同步 I/O 阻塞事件循环，其他工单无法推进"

    def event(**kwargs):
        if node_name != "review_save":
            slow_io()
        return {"event_id": 1}

    def save_review(state, payload, review_round):
        if node_name == "review_save":
            slow_io()
        return {**payload, "review_round": review_round}

    async def model(schema, *args, **kwargs):
        if schema is AgentDecision:
            return AgentDecision(issue_type="OTHER", summary="咨询", request_kind="CONSULTATION")
        return AgentResult(issue_type="OTHER", summary="咨询", analysis="依据规则", solution="答复",
                           resolution_kind="ANSWER")

    monkeypatch.setattr(events, "emit_event", event)
    monkeypatch.setattr(analyze, "ainvoke_structured", model)
    monkeypatch.setattr(result, "ainvoke_structured", model)
    monkeypatch.setattr(retrieve, "retrieve", lambda *args, **kwargs: [])
    monkeypatch.setattr(tools, "QUERY_TOOLS", {"query_ticket_history": SimpleNamespace(invoke=lambda args: {"success": True})})
    monkeypatch.setattr(result, "QUERY_TOOLS", {"query_user_memory": SimpleNamespace(invoke=lambda args: {"success": True})})
    monkeypatch.setattr("app.services.agent_task_service.save_review", save_review)
    monkeypatch.setattr(human, "interrupt", lambda payload: {"approved": True})
    functions = {"analyze": analyze.analyze_ticket, "retrieve": retrieve.retrieve_knowledge,
                 "tools": tools.execute_tools, "result": result.generate_result,
                 "human": human.human_review, "review_save": human.human_review}
    state = {"ticket_id": 1, "agent_run_id": 2, "user_id": 1, "content": "咨询规则",
             "decision": {"request_kind": "CONSULTATION"}}

    async def other_ticket():
        while not entered.is_set():
            await asyncio.sleep(0.001)
        release.set()

    token = execution_claim.set(claim)
    try:
        await asyncio.wait_for(asyncio.gather(functions[node_name](state), other_ticket()), timeout=5)
    finally:
        release.set()
        execution_claim.reset(token)
    assert observed
    assert all(thread_id != loop_thread and saved_claim == claim for thread_id, saved_claim in observed)


@pytest.mark.parametrize("failure", [OSError("数据库暂时不可达"), ExecutionLost("租约已过期")])
def test_heartbeat_failure_exits_even_when_cause_is_not_expiry(monkeypatch, failure):
    """续租无法确认与确定过期均退出；不能把所有心跳失败都解释成租约到期。"""
    from app import worker

    claim = ExecutionClaim(1, 2, 3, "test-worker")

    def renew(received):
        assert received == claim
        raise failure

    def exit_process(code):
        raise SystemExit(code)

    monkeypatch.setattr(worker.tasks, "renew_execution_lease", renew)
    monkeypatch.setattr(worker.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as stopped:
        worker.keep_lease(claim, SimpleNamespace(wait=lambda interval: False))
    assert stopped.value.code == 2
