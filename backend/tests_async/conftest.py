"""真实 HTTP、MySQL、SQLite 和独立 worker 的测试装置；只替换模型输出。"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def eventually(check, *, timeout=25, description="等待异步操作"):
    """按可观察状态轮询；超时输出最后一个状态，而非固定睡眠猜执行进度。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"{description}超时；最后结果={last!r}")


@pytest.fixture(scope="session")
def sandbox():
    """没有隔离启动器的专用标识时直接报错，避免把测试写进用户库。"""
    name = os.environ.get("ASYNC_TEST_DATABASE", "")
    url = make_url(os.environ.get("DATABASE_URL", ""))
    if not re.fullmatch(r"agent_async_test_[0-9a-f]{32}", name) or url.database != name:
        pytest.fail("请使用 python scripts/run_async_regression.py，禁止直接对业务库运行本套测试")
    engine = create_engine(url, pool_pre_ping=True)
    yield engine
    engine.dispose()


def query(engine, sql, **params):
    """短连接读取真实持久化状态。"""
    with engine.connect() as db:
        return [dict(row) for row in db.execute(text(sql), params).mappings()]


def execute(engine, sql, **params):
    """仅在测试库内制造明确的故障前置条件。"""
    with engine.begin() as db:
        db.execute(text(sql), params)


class Processes:
    """持有并回收本用例创建的进程，日志只存在启动器的临时目录。"""

    def __init__(self, folder):
        self.folder = folder
        self.entries = []

    def start(self, args, *, env=None, label="process"):
        path = self.folder / f"{label}-{len(self.entries)}.log"
        handle = path.open("w")
        process = subprocess.Popen(args, cwd=ROOT, env=env or os.environ.copy(),
                                   stdout=handle, stderr=handle, stdin=subprocess.DEVNULL)
        self.entries.append((process, handle, path))
        return process

    def worker(self, *, action="apply_refund", delay=0.05, fail=None, fatal=None, fault=None,
               concurrency=None, review_timeout=None, review_reminder=None):
        """桩运行在真正 worker 内，不向 API 注册旧的进程内测试钩子。"""
        env = os.environ.copy()
        env.update(AGENT_STUB_BUSINESS_ACTION=action, AGENT_STUB_DELAY_SECONDS=str(delay),
                   AGENT_STUB_FAIL_TICKETS=str(fail or ""), AGENT_STUB_FATAL_TICKETS=str(fatal or ""))
        if concurrency is not None:
            env["AGENT_MAX_CONCURRENCY"] = str(concurrency)
        if review_timeout is not None:
            env["AGENT_REVIEW_TIMEOUT_SECONDS"] = str(review_timeout)
        if review_reminder is not None:
            env["AGENT_REVIEW_REMINDER_SECONDS"] = str(review_reminder)
        module = "app.worker"
        if fault:
            module = "tests_async.fault_worker"
            env["ASYNC_TEST_FAULT"] = fault
        return self.start([sys.executable, "-m", module], env=env, label="worker")

    @staticmethod
    def stop(process, *, kill=False):
        if process.poll() is None:
            process.kill() if kill else process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def logs(self):
        return "\n".join(path.read_text(errors="replace")[-5000:] for _, _, path in self.entries)

    def close(self):
        for process, handle, _ in reversed(self.entries):
            self.stop(process)
            handle.close()


@pytest.fixture
def processes(sandbox, tmp_path):
    manager = Processes(tmp_path)
    yield manager
    manager.close()


@dataclass
class Client:
    raw: httpx.Client
    user_id: int = 0

    def request(self, method, path, **kwargs):
        return self.raw.request(method, path, **kwargs)

    def ok(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        assert response.is_success, f"{method} {path}: {response.status_code} {response.text[:500]}"
        return response.json()

    def status(self, method, path, **kwargs):
        return self.request(method, path, **kwargs).status_code


class App:
    """每例独立 API 进程与账号；同一数据库中的不同用例不会共用活动任务。"""

    def __init__(self, base, engine, manager):
        self.base, self.engine, self.processes = base, engine, manager
        self.clients = []
        self.user = self.client(register=True)
        self.other = self.client(register=True)
        self.staff = self.client("service01", "123456")
        self.admin = self.client("admin", "admin123")

    def client(self, username=None, password="123456", *, register=False):
        raw = httpx.Client(base_url=self.base + "/api/", trust_env=False, timeout=15)
        self.clients.append(raw)
        client = Client(raw)
        if register:
            username = "async_" + uuid.uuid4().hex[:12]
            client.ok("POST", "auth/register", json={"username": username, "password": password})
        payload = client.ok("POST", "auth/login", json={"username": username, "password": password})
        raw.headers["Authorization"] = "Bearer " + payload["access_token"]
        client.user_id = payload["user"]["user_id"]
        return client

    def scenario(self, *, price=120, paid=True, sale=False):
        product = self.admin.ok("POST", "admin/products", json={
            "name": "异步回归_" + uuid.uuid4().hex[:8], "price": price, "stock": 10})
        pid = product["product_id"]
        self.user.ok("POST", "cart/items", json={"product_id": pid, "quantity": 1})
        order = self.user.ok("POST", "orders", json={})
        oid = order["order_id"]
        if paid:
            self.user.ok("POST", f"orders/{oid}/pay")
        body = {"content": "未发货，不想要了，请全额退款", "order_id": oid}
        if sale:
            record = self.user.ok("POST", "after-sales", json={
                "order_id": oid, "type": "REFUND", "reason": "异步回归全额退款"})
            body["after_sale_id"] = record["after_sale_id"]
        ticket = self.user.ok("POST", "tickets", json=body)
        return {"ticket": ticket["ticket_id"], "order": oid, "product": pid,
                "sale": body.get("after_sale_id")}

    def submit(self, case):
        response = self.user.request("POST", f"tickets/{case['ticket']}/process")
        assert response.status_code == 202, response.text
        return response.json()

    def detail(self, case):
        return self.user.ok("GET", f"tickets/{case['ticket']}")

    def run(self, case):
        rows = query(self.engine, "SELECT * FROM agent_runs WHERE ticket_id=:id ORDER BY id DESC", id=case["ticket"])
        return rows[0] if rows else None

    def wait(self, case, status):
        def check():
            run = self.run(case)
            if run and run["status"] == status:
                return self.detail(case)
            if run and run["status"] in ("FAILED", "REJECTED", "COMPLETED"):
                raise AssertionError(f"预期 {status}，实际 {run['status']}：{run['error']}\n{self.processes.logs()}")
            return None
        return eventually(check, description=f"任务进入 {status}")

    def approval(self, case, **overrides):
        run = self.run(case)
        return {"expected_run_id": run["id"], "review_round": run["review_round"], **overrides}

    def refund_count(self, case):
        return query(self.engine, "SELECT COUNT(*) AS n FROM refund_records WHERE order_id=:id", id=case["order"])[0]["n"]


@pytest.fixture
def app(sandbox, processes):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    process = processes.start([sys.executable, "-m", "uvicorn", "app.main:app",
                               "--host", "127.0.0.1", "--port", str(port)], label="api")

    def ready():
        assert process.poll() is None, processes.logs()
        try:
            response = httpx.get(base + "/health", trust_env=False, timeout=0.5)
            return response.status_code == 200 and response.json()["database"] == os.environ["ASYNC_TEST_DATABASE"]
        except httpx.TransportError:
            return False

    eventually(ready, description="API 启动")
    instance = App(base, sandbox, processes)
    yield instance
    # 先停止本例的 worker，防止下一例领取本例故意留下的排队/故障任务。
    processes.close()
    execute(sandbox, "UPDATE agent_runs SET status='FAILED', worker_id=NULL, lease_until=NULL "
            "WHERE user_id=:id AND status IN ('PENDING','RUNNING','WAITING_HUMAN')", id=instance.user.user_id)
    for client in instance.clients:
        client.close()
