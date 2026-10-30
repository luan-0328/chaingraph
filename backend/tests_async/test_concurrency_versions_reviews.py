"""SQLite 并发、部署版本边界及审批期限的真实进程回归。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

import httpx
import pytest
from sqlalchemy.engine import make_url

from tests_async.conftest import eventually, execute, query
from tests_async.test_worker_flow import assert_refunded_once, waiting


def test_different_tickets_overlap_but_respect_concurrency_limit(app, processes):
    """同时观察两个 RUNNING，第三个仍排队；分别核对 SQLite 状态及业务归属。"""
    cases = [app.scenario() for _ in range(3)]
    submitted = [app.submit(case) for case in cases]
    processes.worker(concurrency=2, delay=1)

    def overlapping():
        statuses = [app.run(case)["status"] for case in cases]
        assert statuses.count("RUNNING") <= 2
        return statuses.count("RUNNING") == 2 and statuses.count("PENDING") == 1

    eventually(overlapping, description="两个工单重叠执行且第三个排队")
    for case in cases:
        assert_refunded_once(app, case)
        assert app.run(case)["execution_epoch"] == 1
    with sqlite3.connect(os.environ["CHECKPOINT_DB_PATH"]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        stored = {row[0] for row in db.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    assert {run["thread_id"] for run in submitted} <= stored
    assert len({run["thread_id"] for run in submitted}) == 3


def test_running_ticket_reuses_one_execution(app, processes):
    """并发执行开启后，同一工单追加提交也不能产生第二条运行。"""
    case = app.scenario()
    original = app.submit(case)
    processes.worker(concurrency=4, delay=0.6)
    eventually(lambda: app.run(case)["status"] == "RUNNING", description="任务已领取")
    for _ in range(5):
        payload = app.submit(case)
        assert payload["used_existing_run"] and payload["agent_run_id"] == original["agent_run_id"]
    assert_refunded_once(app, case)
    assert len(query(app.engine, "SELECT id FROM agent_runs WHERE ticket_id=:id", id=case["ticket"])) == 1


def test_checkpoint_file_lock_blocks_different_database_identity(app, processes):
    """同一个 SQLite 文件即使换数据库连接标识，也不能被第二个 worker 打开。"""
    case = app.scenario()
    app.submit(case)
    first = processes.worker(delay=2)
    eventually(lambda: app.run(case)["status"] == "RUNNING", description="首个 worker 持有文件")
    url = make_url(os.environ["DATABASE_URL"])
    alternate = url.set(host="127.0.0.1" if url.host == "localhost" else "localhost")
    env = os.environ.copy()
    env["DATABASE_URL"] = alternate.render_as_string(hide_password=False)
    key = hashlib.sha256(f"{alternate.host}:{alternate.port or 3306}/{alternate.database}".encode()).hexdigest()[:16]
    lock = Path(__file__).resolve().parents[1] / "data" / f"agent-worker-{key}.lock"
    second = processes.start([sys.executable, "-m", "app.worker"], env=env, label="shared-checkpoint")
    try:
        eventually(lambda: second.poll() is not None, description="第二个 Checkpoint 写入者被拒绝")
        assert second.returncode != 0 and first.poll() is None
        assert "业务库或 Checkpoint 已有 worker" in processes.logs()
    finally:
        processes.stop(second)
        lock.unlink(missing_ok=True)


def test_submitted_task_records_execution_contract(app):
    """保存可复核的指纹和无密钥配置，而非仅记录一个展示用版本号。"""
    case = app.scenario()
    app.submit(case)
    detail = app.detail(case)["agent_run"]
    spec = detail["execution_spec"]
    assert len(detail["flow_version"]) == 64 and spec["version"] == detail["flow_version"]
    assert len(spec["code"]) == 64 and len(spec["prompt"]) == 64
    assert spec["model"]["name"] and "langgraph" in spec["dependencies"]
    assert "api_key" not in json.dumps(spec).lower()


@pytest.mark.parametrize("version", [None, "0" * 64])
def test_unknown_or_changed_flow_cannot_execute(app, processes, version):
    """旧数据缺少版本或新部署不匹配时，不得从头猜测执行任务。"""
    case = app.scenario()
    app.submit(case)
    execute(app.engine, "UPDATE agent_runs SET flow_version=:version WHERE id=:id",
            version=version, id=app.run(case)["id"])
    processes.worker()
    detail = app.wait(case, "FAILED")
    assert "AGENT_VERSION_MISMATCH" in detail["agent_run"]["error"]
    assert app.refund_count(case) == 0
    assert not query(app.engine, "SELECT id FROM agent_run_events WHERE agent_run_id=:id AND event_type='ANALYZING'",
                     id=app.run(case)["id"])


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_changed_version_blocks_review_but_allows_takeover(app, processes, decision):
    """不兼容方案不能恢复；原始方案与 Checkpoint 保留，客服仍可接管。"""
    case, _ = waiting(app, processes)
    execute(app.engine, "UPDATE agent_runs SET flow_version=:version WHERE id=:id",
            version="0" * 64, id=app.run(case)["id"])
    run = app.detail(case)["agent_run"]
    assert not run["approval_available"] and "版本" in run["approval_blocked_reason"]
    assert app.staff.status("POST", f"tickets/{case['ticket']}/{decision}", json=app.approval(case)) == 409
    app.staff.ok("POST", f"tickets/{case['ticket']}/takeover", json={})
    assert app.run(case)["status"] == "REJECTED" and app.refund_count(case) == 0


@pytest.mark.parametrize("configuration,value", [("LLM_MODEL", "changed-model"), ("REFUND_AUTO_APPROVE_LIMIT", "501")])
def test_worker_cannot_silently_change_task_configuration(app, processes, configuration, value):
    """API 提交后即使代码没变，模型或核心业务配置变化也必须拒绝执行。"""
    case = app.scenario()
    app.submit(case)
    env = os.environ.copy()
    env[configuration] = value
    processes.start([sys.executable, "-m", "app.worker"], env=env, label="changed-config")
    detail = app.wait(case, "FAILED")
    assert "AGENT_VERSION_MISMATCH" in detail["agent_run"]["error"]
    assert app.refund_count(case) == 0


def test_real_deadline_reminds_once_then_hands_off_without_refund(app, processes):
    """等待真实截止时间，站内提醒只写一次，到期转人工但不擅自关闭售后申请。"""
    from app.services.agent_task_service import sweep_reviews
    case = app.scenario(price=600, sale=True)
    app.submit(case)
    processes.worker(review_timeout=4, review_reminder=1)
    detail = app.wait(case, "WAITING_HUMAN")
    assert detail["agent_run"]["review_deadline_at"]
    assert detail["agent_run"]["approval_available"]
    run_id = app.run(case)["id"]
    eventually(lambda: app.run(case)["review_reminded_at"], description="真实审批提醒")
    sweep_reviews()
    detail = app.wait(case, "FAILED")
    assert detail["status"] == "WAITING_HUMAN" and detail["agent_run_id"] is None
    assert detail["handling_mode"] == "HUMAN"
    assert "REVIEW_EXPIRED" in detail["agent_run"]["error"]
    sweep_reviews()
    for event in ("REVIEW_REMINDER", "REVIEW_EXPIRED"):
        assert len(query(app.engine, "SELECT id FROM agent_run_events WHERE agent_run_id=:id AND event_type=:event",
                         id=run_id, event=event)) == 1
    assert app.refund_count(case) == 0
    assert query(app.engine, "SELECT status FROM after_sales WHERE id=:id", id=case["sale"])[0]["status"] in ("APPLYING", "PROCESSING")
    assert app.staff.status("POST", f"tickets/{case['ticket']}/approve",
                            json={"expected_run_id": run_id, "review_round": 1}) == 409


def test_deadline_cannot_be_extended_by_repeated_submission(app, processes):
    """重复请求复用原方案，不刷新审批时钟。"""
    case, _ = waiting(app, processes)
    before = app.run(case)["review_deadline_at"]
    app.submit(case)
    assert app.run(case)["review_deadline_at"] == before


def test_expired_approval_racing_sweeper_never_executes(app, processes):
    """到期扫描和 HTTP 批准争抢行锁时，过期决定在任一顺序下都不能生效。"""
    from app.services.agent_task_service import sweep_reviews
    case, _ = waiting(app, processes)
    body = app.approval(case)
    execute(app.engine, "UPDATE agent_runs SET review_deadline_at=UTC_TIMESTAMP()-INTERVAL 1 SECOND WHERE id=:id",
            id=body["expected_run_id"])
    barrier = threading.Barrier(2)

    def approve():
        with httpx.Client(trust_env=False, timeout=15) as client:
            barrier.wait(timeout=10)
            return client.post(f"{app.base}/api/tickets/{case['ticket']}/approve", json=body,
                               headers={"Authorization": app.staff.raw.headers["Authorization"]}).status_code

    def expire():
        barrier.wait(timeout=10)
        return sweep_reviews()

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(approve)
        expiry = pool.submit(expire)
        assert approval.result() == 409
        assert expiry.result()["expired"] == 1
    assert app.refund_count(case) == 0 and app.run(case)["status"] == "FAILED"
