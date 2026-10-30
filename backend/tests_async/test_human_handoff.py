"""主动转人工、明确指令、手工交流和运行中安全交接的回归。"""

import os
from pathlib import Path

import pytest

from app.services.handoff_service import is_handoff_command
from tests_async.conftest import eventually, query
from tests_async.test_worker_flow import assert_refunded_once, waiting


@pytest.mark.parametrize("text,expected", [
    ("转人工", True), ("请帮我转接人工客服！", True), ("我要人工客服", True),
    ("  联系 人工客服。 ", True), ("人工客服", True), ("麻烦转人工一下", True),
    ("不要转人工", False), ("不用人工客服，我先问问", False),
    ("人工客服几点上班？", False), ("你说的转人工是什么意思", False),
    ("“转人工”是一个选项", False), ("如果有问题再转人工", False),
    ("退款并转人工", False), ("帮我批准退款", False),
])
def test_only_unambiguous_short_commands_are_routed(text, expected):
    assert is_handoff_command(text) is expected


def request(app, case):
    return app.user.ok("POST", f"tickets/{case['ticket']}/request-human", json={})


def test_new_ticket_short_command_bypasses_model(app):
    detail = app.user.ok("POST", "tickets", json={"content": "请帮我转人工"})
    assert detail["handling_mode"] == "HUMAN" and detail["status"] == "WAITING_HUMAN"
    assert detail["agent_runs"] == []
    assert app.user.status("POST", f"tickets/{detail['ticket_id']}/process") == 409


def test_button_handoff_is_owned_and_idempotent(app):
    case = app.scenario()
    assert app.other.status("POST", f"tickets/{case['ticket']}/request-human", json={}) == 403
    first = request(app, case)
    second = request(app, case)
    assert first["handling_mode"] == second["handling_mode"] == "HUMAN"
    assert len(first["messages"]) == len(second["messages"])
    assert not first["agent_runs"]
    assert app.user.status("POST", f"tickets/{case['ticket']}/process") == 409
    assert app.user.status("POST", f"tickets/{case['ticket']}/return-to-agent") == 403


def test_message_command_and_negative_message_have_distinct_effects(app):
    case = app.scenario()
    app.user.ok("POST", f"tickets/{case['ticket']}/messages", json={"content": "不要转人工"})
    assert app.detail(case)["handling_mode"] == "AGENT"
    app.user.ok("POST", f"tickets/{case['ticket']}/messages", json={"content": "转人工"})
    assert app.detail(case)["handling_mode"] == "HUMAN"
    assert app.run(case) is None


def test_queued_and_paused_tasks_transfer_without_execution(app, processes):
    case = app.scenario()
    app.submit(case)
    assert request(app, case)["handling_mode"] == "HUMAN"
    assert app.run(case)["status"] == "REJECTED" and app.run(case)["execution_epoch"] == 0
    paused, _ = waiting(app, processes)
    old = app.approval(paused)
    assert request(app, paused)["handling_mode"] == "HUMAN"
    assert app.staff.status("POST", f"tickets/{paused['ticket']}/approve", json=old) == 409
    assert app.refund_count(case) == app.refund_count(paused) == 0


def test_manual_conversation_requires_claim_and_explicit_return(app, processes):
    case = app.scenario()
    request(app, case)
    assert app.staff.status("POST", f"customer-service/tickets/{case['ticket']}/messages", json={"content": "您好"}) == 403
    app.staff.ok("POST", f"customer-service/tickets/{case['ticket']}/assign", json={})
    app.user.ok("POST", f"tickets/{case['ticket']}/messages", json={"content": "我需要核实退款"})
    app.staff.ok("POST", f"customer-service/tickets/{case['ticket']}/messages", json={"content": "我是人工客服，已了解您的情况"})
    detail = app.detail(case)
    assert detail["handling_mode"] == "HUMAN" and detail["status"] == "PROCESSING"
    assert detail["messages"][-1]["sender_type"] == "CUSTOMER_SERVICE"
    assert app.user.status("POST", f"tickets/{case['ticket']}/process") == 409
    assert app.admin.status("POST", f"tickets/{case['ticket']}/return-to-agent") == 403
    app.staff.ok("POST", f"tickets/{case['ticket']}/return-to-agent")
    app.submit(case)
    processes.worker()
    assert_refunded_once(app, case)


def test_running_handoff_stops_only_requested_ticket(app, processes):
    case, other = app.scenario(), app.scenario()
    app.submit(case)
    app.submit(other)
    worker = processes.worker(delay=1.2, concurrency=2)
    eventually(lambda: app.run(case)["current_node"] == "analyze_ticket" and app.run(other)["status"] == "RUNNING")
    detail = request(app, case)
    assert detail["handling_mode"] == "HUMAN_PENDING" and app.run(case)["status"] == "RUNNING"
    assert app.staff.status("POST", f"customer-service/tickets/{case['ticket']}/assign", json={}) == 409
    detail = app.wait(case, "REJECTED")
    assert detail["handling_mode"] == "HUMAN" and app.refund_count(case) == 0
    assert not any(m["sender_type"] == "AGENT" for m in detail["messages"])
    assert_refunded_once(app, other)
    assert worker.poll() is None


@pytest.mark.parametrize("fault,refunds", [("pause_before_refund", 0), ("pause_after_refund", 1)])
def test_handoff_linearizes_with_business_commit(app, processes, fault, refunds):
    case = app.scenario()
    app.submit(case)
    marker = Path(os.environ["ASYNC_TEST_FOLDER"]) / "handoff-ready"
    marker.unlink(missing_ok=True)
    worker = processes.worker(fault=fault)
    eventually(marker.exists, description="到达业务事务测试窗口")
    assert request(app, case)["handling_mode"] == "HUMAN_PENDING"
    detail = app.wait(case, "REJECTED")
    assert detail["handling_mode"] == "HUMAN" and app.refund_count(case) == refunds
    stock = query(app.engine, "SELECT stock FROM products WHERE id=:id", id=case["product"])[0]["stock"]
    assert stock == (10 if refunds else 9)
    if refunds:
        assert any("已记录该订单退款" in m["content"] for m in detail["messages"])
    assert worker.poll() is None


def test_handoff_survives_worker_crash_without_requiring_checkpoint(app, processes):
    case = app.scenario()
    app.submit(case)
    worker = processes.worker(delay=3)
    eventually(lambda: app.run(case)["current_node"] == "analyze_ticket")
    request(app, case)
    processes.stop(worker, kill=True)
    processes.worker()
    detail = app.wait(case, "REJECTED")
    assert detail["handling_mode"] == "HUMAN" and detail["agent_run"]["execution_epoch"] == 2
    assert app.refund_count(case) == 0


@pytest.mark.parametrize("fault", ["human_plan", "semantic_handoff"])
def test_agent_inability_and_semantic_request_enter_manual_queue(app, processes, fault):
    case = app.scenario()
    app.submit(case)
    worker = processes.worker(fault=fault)
    detail = app.wait(case, "REJECTED")
    assert detail["handling_mode"] == "HUMAN" and detail["status"] == "WAITING_HUMAN"
    assert not detail["agent_run"]["approval_available"]
    assert app.refund_count(case) == 0 and worker.poll() is None
