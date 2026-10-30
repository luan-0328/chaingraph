"""异步契约回归：HTTP 提交、真实子进程执行、审批、崩溃恢复和业务不变量。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import os
import sys
import threading
import time

import httpx
import pytest
from sqlalchemy import text

from tests_async.conftest import eventually, execute, query


def waiting(app, processes, *, sale=False):
    """创建超自动退款上限的任务，等真正 interrupt 保存后停止 worker。"""
    case = app.scenario(price=600, sale=sale)
    app.submit(case)
    worker = processes.worker()
    detail = app.wait(case, "WAITING_HUMAN")
    processes.stop(worker)
    assert app.refund_count(case) == 0
    return case, detail["agent_run"]


def assert_refunded_once(app, case, *, approved=False):
    """同时核对业务流水、库存、任务归属和对话，避免只看到 HTTP 成功。"""
    detail = app.wait(case, "COMPLETED")
    assert detail["status"] == "RESOLVED"
    assert detail["agent_run_id"] is None
    assert detail["agent_status"] == "IDLE"
    run = app.run(case)
    assert run["lease_until"] is None and run["worker_id"] is None
    assert run["current_node"] is None
    refunds = query(app.engine, "SELECT * FROM refund_records WHERE order_id=:id", id=case["order"])
    assert len(refunds) == 1 and refunds[0]["status"] == "SUCCESS"
    assert float(refunds[0]["amount"]) in (120, 600)
    assert bool(refunds[0]["approved_by"]) == approved
    order = app.user.ok("GET", f"orders/{case['order']}")
    assert order["payment_status"] == "REFUNDED"
    assert query(app.engine, "SELECT stock FROM products WHERE id=:id", id=case["product"])[0]["stock"] == 10
    assert query(app.engine, "SELECT status FROM after_sales WHERE order_id=:id", id=case["order"])[0]["status"] == "COMPLETED"
    # 等待审核时可以有独立提示；只禁止重复写入最终处理答复。
    reply_key = f"run:{run['id']}:ticket:{case['ticket']}:reply"
    replies = query(app.engine, "SELECT content FROM ticket_messages WHERE dedup_key=:key", key=reply_key)
    assert len(replies) == 1
    assert replies[0]["content"] == detail["agent_run"]["result"]["solution"]
    events = query(app.engine, "SELECT event_type FROM agent_run_events WHERE agent_run_id=:id", id=run["id"])
    assert sum(e["event_type"] == "COMPLETED" for e in events) == 1
    return detail


def test_submit_is_202_and_persists_without_worker(app):
    """没有 worker 时只能排队，接口返回不能冒充处理完成。"""
    case = app.scenario()
    payload = app.submit(case)
    assert payload["status"] == "PENDING"
    assert app.run(case)["execution_epoch"] == 0
    assert app.run(case)["thread_id"] == payload["thread_id"]
    assert app.detail(case)["status"] == "PROCESSING"
    assert app.refund_count(case) == 0
    assert app.user.ok("GET", f"orders/{case['order']}")["payment_status"] == "PAID"


def test_concurrent_submission_creates_one_run(app):
    """真正同时发 HTTP 请求，确认复用同一个持久化任务。"""
    case = app.scenario()
    barrier = threading.Barrier(8)
    authorization = app.user.raw.headers["Authorization"]

    def submit(_):
        with httpx.Client(trust_env=False, timeout=15) as client:
            barrier.wait(timeout=10)
            response = client.post(f"{app.base}/api/tickets/{case['ticket']}/process",
                                   headers={"Authorization": authorization})
            assert response.status_code == 202, response.text
            return response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        payloads = list(pool.map(submit, range(8)))
    assert len({p["agent_run_id"] for p in payloads}) == 1
    assert sum(not p["used_existing_run"] for p in payloads) == 1
    assert len(query(app.engine, "SELECT id FROM agent_runs WHERE ticket_id=:id", id=case["ticket"])) == 1


@pytest.mark.parametrize("operation", ["process", "stream"])
def test_another_user_cannot_process_or_read_progress(app, operation):
    """处理和事件订阅都必须校验工单归属。"""
    case = app.scenario()
    method = "POST" if operation == "process" else "GET"
    assert app.other.status(method, f"tickets/{case['ticket']}/{operation}") == 403


@pytest.mark.parametrize("operation,body", [("close", {}), ("resolve", {"solution": "不能提前结单"}), ("takeover", {})])
def test_pending_task_cannot_be_closed_resolved_or_taken_over(app, operation, body):
    """任务仍在排队时，人工页面不得越过任务生命周期直接结束工单。"""
    case = app.scenario()
    app.submit(case)
    assert app.staff.status("POST", f"tickets/{case['ticket']}/{operation}", json=body) == 409
    assert app.run(case)["status"] == "PENDING"


def test_worker_executes_automatic_refund_and_retains_memory(app, processes):
    """完整低额退款链路，不替换 Tool、Service、事务或 LangGraph。"""
    case = app.scenario()
    app.submit(case)
    processes.worker()
    detail = assert_refunded_once(app, case)
    assert detail["agent_run"]["used_rag"] and detail["agent_run"]["used_tool"]
    assert app.user.status("POST", f"orders/{case['order']}/pay") == 409
    assert app.user.status("POST", f"tickets/{case['ticket']}/process") == 409
    assert query(app.engine, "SELECT id FROM user_memories WHERE source=:source",
                 source=f"ticket:{case['ticket']}")


def test_waiting_reuses_run_and_checks_approval_identity(app, processes):
    """审核依赖 run 和轮次，普通用户不能批准自己的退款。"""
    case, run = waiting(app, processes)
    reused = app.submit(case)
    assert reused["used_existing_run"] and reused["agent_run_id"] == run["agent_run_id"]
    body = app.approval(case)
    assert app.user.status("POST", f"tickets/{case['ticket']}/approve", json=body) == 403
    for wrong in ({"expected_run_id": body["expected_run_id"] + 999}, {"review_round": body["review_round"] + 1}):
        assert app.staff.status("POST", f"tickets/{case['ticket']}/approve", json={**body, **wrong}) == 409
    assert app.run(case)["status"] == "WAITING_HUMAN"
    assert app.refund_count(case) == 0


def test_duplicate_approval_resumes_original_thread_after_restart(app, processes):
    """审批先落库；重启后仍恢复原图，重复审批不能重复退款或反转决定。"""
    case, before = waiting(app, processes)
    body = app.approval(case)
    for _ in range(2):
        app.staff.ok("POST", f"tickets/{case['ticket']}/approve", json=body)
    assert app.run(case)["status"] == "PENDING" and app.refund_count(case) == 0
    assert app.staff.status("POST", f"tickets/{case['ticket']}/reject", json={**body, "reason": "不能反转"}) == 409
    processes.worker()
    detail = assert_refunded_once(app, case, approved=True)
    assert detail["agent_run"]["agent_run_id"] == before["agent_run_id"]
    assert detail["agent_run"]["thread_id"] == before["thread_id"]
    assert detail["agent_run"]["execution_epoch"] == 2


def test_rejection_never_refunds_and_returns_to_staff(app, processes):
    """驳回必须真正恢复原中断并交接，不能把售后标成退款成功。"""
    case, _ = waiting(app, processes, sale=True)
    app.staff.ok("POST", f"tickets/{case['ticket']}/reject", json={**app.approval(case), "reason": "测试驳回"})
    processes.worker()
    detail = app.wait(case, "REJECTED")
    assert detail["status"] == "PROCESSING" and detail["agent_run_id"] is None
    assert app.refund_count(case) == 0
    assert app.user.ok("GET", f"orders/{case['order']}")["payment_status"] == "PAID"
    assert query(app.engine, "SELECT status FROM after_sales WHERE id=:id", id=case["sale"])[0]["status"] == "REJECTED"
    assert any("转人工跟进" in m["content"] for m in detail["messages"])


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_order_change_invalidates_old_review(app, processes, decision):
    """审核期间订单变化，旧批准与旧驳回都必须返回冲突并更新审核版本。"""
    case, before = waiting(app, processes)
    body = app.approval(case)
    app.staff.ok("POST", f"customer-service/orders/{case['order']}/ship")
    assert app.staff.status("POST", f"tickets/{case['ticket']}/{decision}", json=body) == 409
    run = app.run(case)
    assert run["review_round"] == before["review_round"] + 1
    assert run["status"] == "WAITING_HUMAN" and app.refund_count(case) == 0


def test_order_change_after_approval_requires_new_interrupt(app, processes):
    """批准已提交但尚未执行时发货，worker 必须重新中断，而不是沿用过期批准。"""
    case, before = waiting(app, processes)
    app.staff.ok("POST", f"tickets/{case['ticket']}/approve", json=app.approval(case))
    app.staff.ok("POST", f"customer-service/orders/{case['order']}/ship")
    processes.worker()
    detail = app.wait(case, "WAITING_HUMAN")
    assert detail["agent_run"]["review_round"] == before["review_round"] + 1
    assert app.refund_count(case) == 0


def test_waiting_takeover_disables_old_approval(app, processes):
    """人工接管移除原任务执行权，旧页面不能再恢复原方案。"""
    case, _ = waiting(app, processes)
    body = app.approval(case)
    first = app.staff.ok("POST", f"tickets/{case['ticket']}/takeover", json={})
    second = app.staff.ok("POST", f"tickets/{case['ticket']}/takeover", json={})
    assert first["assignee_id"] == second["assignee_id"]
    assert first["agent_run_id"] is None and app.run(case)["status"] == "REJECTED"
    assert app.staff.status("POST", f"tickets/{case['ticket']}/approve", json=body) == 409
    assert len(first["messages"]) == len(second["messages"])
    assert app.refund_count(case) == 0


def test_sse_replays_only_events_after_cursor_and_closes(app, processes):
    """事件由 MySQL 回放，重连游标不漏读、不重读，终态流能结束。"""
    case = app.scenario()
    app.submit(case)
    processes.worker()
    app.wait(case, "COMPLETED")
    events = query(app.engine, "SELECT id FROM agent_run_events WHERE agent_run_id=:id ORDER BY id", id=app.run(case)["id"])
    cursor = events[len(events) // 2]["id"]
    with app.user.raw.stream("GET", f"tickets/{case['ticket']}/stream", headers={"Last-Event-ID": str(cursor)}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = list(response.iter_lines())
    ids = [int(line.removeprefix("id: ")) for line in lines if line.startswith("id:")]
    assert ids == [e["id"] for e in events if e["id"] > cursor]
    frames = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data:")]
    assert frames[-1]["type"] == "done" and frames[-1]["status"] == "COMPLETED"


@pytest.mark.parametrize("fault", ["after_refund_commit", "after_action", "before_finish"])
def test_crash_windows_do_not_duplicate_refund_or_reply(app, processes, fault, record_property):
    """跨 MySQL 和 Checkpoint 的三个提交窗口都使用真实进程崩溃验证。"""
    case = app.scenario()
    original = app.submit(case)
    crashed = processes.worker(fault=fault)
    eventually(lambda: crashed.poll() is not None, description=f"故障进程 {fault} 退出")
    assert crashed.returncode == 86, processes.logs()
    assert app.refund_count(case) == 1
    assert app.run(case)["status"] == "RUNNING"
    # 不替换任务状态或租约；新进程按真实租约到期时间自动恢复。
    recovery_started = time.perf_counter()
    processes.worker()
    detail = assert_refunded_once(app, case)
    record_property("crash_window", fault)
    record_property("recovery_seconds_including_restart_and_lease_wait", round(time.perf_counter() - recovery_started, 4))
    assert detail["agent_run"]["agent_run_id"] == original["agent_run_id"]
    assert detail["agent_run"]["thread_id"] == original["thread_id"]
    assert detail["agent_run"]["execution_epoch"] == 2


def test_killed_worker_resumes_saved_nodes_without_reanalysis(app, processes):
    """生成结论时杀死 worker，重新启动应复用已经保存的分析与检索。"""
    case = app.scenario()
    app.submit(case)
    worker = processes.worker(delay=1.5)
    eventually(lambda: app.run(case)["current_node"] == "generate_result", description="进入生成节点")
    processes.stop(worker, kill=True)
    processes.worker()
    assert_refunded_once(app, case)
    events = query(app.engine, "SELECT event_type FROM agent_run_events WHERE agent_run_id=:id", id=app.run(case)["id"])
    assert sum(e["event_type"] == "ANALYZING" for e in events) == 1


def test_recovery_does_not_close_ticket_reopened_by_user(app, processes):
    """任务补收尾不能覆盖用户在崩溃窗口主动重新打开工单的决定。"""
    case = app.scenario()
    app.submit(case)
    crashed = processes.worker(fault="before_finish")
    eventually(lambda: crashed.poll() is not None, description="图完成后崩溃")
    assert crashed.returncode == 86 and app.detail(case)["status"] == "RESOLVED"
    app.user.ok("POST", f"tickets/{case['ticket']}/reopen")
    processes.worker()
    detail = app.wait(case, "COMPLETED")
    assert detail["status"] == "PROCESSING"
    assert app.refund_count(case) == 1
    reply_key = f"run:{app.run(case)['id']}:ticket:{case['ticket']}:reply"
    assert len(query(app.engine, "SELECT id FROM ticket_messages WHERE dedup_key=:key", key=reply_key)) == 1


def test_missing_checkpoint_fails_closed_without_refund(app, processes):
    """审批恢复缺少原 Checkpoint 时，不能猜测执行进度从头重跑。"""
    case, _ = waiting(app, processes)
    app.staff.ok("POST", f"tickets/{case['ticket']}/approve", json=app.approval(case))
    checkpoint = Path(os.environ["CHECKPOINT_DB_PATH"])
    # 换到新空文件，避免破坏其他用例的历史 Checkpoint。
    env = os.environ.copy()
    env["CHECKPOINT_DB_PATH"] = str(processes.folder / "missing.sqlite")
    processes.start([sys.executable, "-m", "app.worker"], env=env, label="missing-checkpoint")
    detail = app.wait(case, "FAILED")
    assert "Checkpoint" in detail["agent_run"]["error"]
    assert app.refund_count(case) == 0 and checkpoint.exists()


@pytest.mark.parametrize("kind", ["retryable", "fatal"])
def test_worker_retries_transient_error_but_not_fatal_error(app, processes, kind):
    """故障由 worker 内的模型桩注入，验证新的持久化重试调度。"""
    case = app.scenario()
    original = app.submit(case)
    processes.worker(**({"fail": case["ticket"]} if kind == "retryable" else {"fatal": case["ticket"]}))
    detail = app.wait(case, "COMPLETED" if kind == "retryable" else "FAILED")
    run = detail["agent_run"]
    assert run["agent_run_id"] == original["agent_run_id"]
    assert run["retry_count"] == (1 if kind == "retryable" else 0)
    if kind == "retryable":
        assert run["execution_epoch"] == 2
        assert_refunded_once(app, case)
    else:
        assert app.refund_count(case) == 0


def test_second_worker_cannot_open_same_business_queue(app, processes):
    """第二个 worker 即使使用不同 Checkpoint 路径，也必须被同机锁挡住。"""
    case = app.scenario()
    app.submit(case)
    first = processes.worker(delay=2)
    eventually(lambda: app.run(case)["status"] == "RUNNING", description="首个 worker 领取任务")
    env = os.environ.copy()
    env["CHECKPOINT_DB_PATH"] = str(processes.folder / "other.sqlite")
    second = processes.start([sys.executable, "-m", "app.worker"], env=env, label="second-worker")
    eventually(lambda: second.poll() is not None, description="第二个 worker 拒绝启动")
    assert second.returncode != 0 and first.poll() is None
    assert "请先停止旧 worker" in processes.logs()


def test_stale_execution_claim_cannot_write_after_epoch_changes(app):
    """实际领取新代次后，旧凭证必须在事务入口失败，避免旧线程继续写库。"""
    from app.core.database import session_scope
    from app.core.execution import ExecutionClaim, ExecutionLost, execution_claim
    from app.services import agent_task_service as tasks

    case = app.scenario()
    app.submit(case)
    old = tasks.claim_agent_run("old-worker")
    execute(app.engine, "UPDATE agent_runs SET lease_until=UTC_TIMESTAMP()-INTERVAL 1 SECOND WHERE id=:id", id=old["id"])
    new = tasks.claim_agent_run("new-worker")
    assert new["id"] == old["id"] and new["execution_epoch"] == old["execution_epoch"] + 1
    token = execution_claim.set(ExecutionClaim(case["ticket"], old["id"], old["execution_epoch"], "old-worker"))
    try:
        with pytest.raises(ExecutionLost):
            with session_scope() as db:
                db.execute(text("UPDATE products SET stock=0 WHERE id=:id"), {"id": case["product"]})
    finally:
        execution_claim.reset(token)
    assert query(app.engine, "SELECT stock FROM products WHERE id=:id", id=case["product"])[0]["stock"] == 9
