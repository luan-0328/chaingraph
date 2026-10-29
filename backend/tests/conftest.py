"""pytest 公共装置：真实 MySQL + 真实 HTTP 服务实例。

设计原则（为什么是"起真服务"而不是 mock）
----------------------------------------
这批测试要验的东西全都**只存在于真实运行环境里**：

    · 并发竞态        —— 需要真实 MySQL 的行锁 / 唯一索引 / 条件更新的 affected_rows
    · 事务与回滚      —— mock 掉数据库就只剩"我以为会回滚"
    · Agent 断点续跑  —— 需要真的 LangGraph Checkpoint 与真的重新编译图
    · HITL 恢复       —— 需要真的 interrupt() 暂停 + Command(resume=...) 恢复

mock 掉这些之后测试仍然会绿，但它证明的是"mock 按我的想象工作"。
所以这里选择起真服务：conftest 自己拉起被测实例（和应用生产启动方式完全一致），
只用**确定性 LLM 桩**替换模型调用（不花钱、不抖动，见下）。

两个实例
--------
    business_server  :8021  普通实例：业务/并发/权限测试打它（不碰 LLM）
    agent_server     :8022  带确定性 LLM 桩（AGENT_STUB_LLM=1）：Agent 相关测试打它

为什么要分开：桩实例会挂载无鉴权的 `/api/__test__/*`（登记哪张工单走 HITL、
给某个节点注入故障），这些接口绝不能出现在被测"业务正确性"的实例上。

不可用就跳过
------------
MySQL 连不上 → 整个 tests/integration 标记为 skip（脚本形态的验收照常可跑）。
这样 CI 里没有数据库时不会一片红，而是明确告诉你"没跑，因为没库"。

测试数据会留在库里（明确取舍）
------------------------------
这些用例是**真写库**的（要的正是真实事务与约束），所以每跑一轮会在
`shop_ticket` 里留下约 90 个 `pytest_*` 账号及其订单/工单/退款记录。
`TestStockRace` 的 7 个参数化库存用例会额外建 460 个 `pytest_race*` 账号。

为什么不做"跑完自动清理"：这批数据里有真实的支付/退款/幂等键/Agent 事件，
按外键顺序删干净既啰嗦又容易删漏（本项目没有物理外键，漏删就是脏数据）。
与其做一个半可靠的自动清理，不如：
    1. 会话结束时**打印**这次新增了多少测试数据（下面 session 级 fixture），
       不让它悄悄膨胀；
    2. 提供 `scripts/cleanup_test_data.py` 让你在想清理时显式清一次。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST = "127.0.0.1"
BUSINESS_PORT = 8021
AGENT_PORT = 8022
CHAIN_PORT = 8023

#: 桩的"模型调用"耗时：把并发窗口撑开，让竞态稳定可复现（真实 LLM 抖起来测不准）
STUB_DELAY = "0.8"

CS_LOGIN = {"username": "service01", "password": "123456"}
ADMIN_LOGIN = {"username": "admin", "password": "admin123"}
USER_PASSWORD = "123456"

pytestmark = pytest.mark.integration


# ======================================================================
# 数据库
# ======================================================================
def db_params() -> dict:
    from app.core.config import settings

    url = urlparse(settings.database_url.replace("mysql+pymysql://", "mysql://"))
    return {
        "host": url.hostname,
        "port": url.port or 3306,
        "user": url.username,
        "password": url.password,
        "database": (url.path or "/").lstrip("/"),
        "charset": "utf8mb4",
    }


def db_connect():
    import pymysql

    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **db_params())


def query_one(sql: str, params: tuple = ()) -> dict | None:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def query_all(sql: str, params: tuple = ()) -> list[dict]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()) -> int:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            affected = cur.execute(sql, params)
        conn.commit()
        return int(affected)
    finally:
        conn.close()


def database_available() -> tuple[bool, str]:
    try:
        query_one("SELECT 1 AS ok")
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ======================================================================
# 被测服务实例
# ======================================================================
def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((HOST, port)) == 0


@dataclass
class Server:
    """一个被测实例（自己拉起、用完关掉）。"""

    port: int
    base_url: str
    process: subprocess.Popen | None = None
    logs: list[str] = field(default_factory=list)
    _reader: threading.Thread | None = None

    @property
    def api(self) -> str:
        return f"{self.base_url}/api"

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况
            self.process.kill()
        self.process = None


def _start_server(port: int, env_extra: dict[str, str]) -> Server:
    """启动一个虚拟环境里的 uvicorn 实例，并等它真的能响应才返回。

    日志用独立线程持续读取（管道不读会被写满，进程会卡死在下一次 print 上）。
    """
    server = Server(port=port, base_url=f"http://{HOST}:{port}")

    if _port_open(port):
        # 已经有人在跑（开发者手起的实例）：直接复用，并在结束时不要关掉它
        print(f"[conftest] 复用已运行的实例 :{port}")
        return server

    env = os.environ.copy()
    env.update(env_extra)
    # 桩实例不要继承"真实 LLM"的配置，避免任何一次意外调用花到真钱
    if env_extra.get("AGENT_STUB_LLM") == "1":
        env["LLM_API_KEY"] = ""

    server.process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            HOST,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def pump() -> None:
        assert server.process is not None and server.process.stdout is not None
        for line in server.process.stdout:
            server.logs.append(line.rstrip())

    server._reader = threading.Thread(target=pump, daemon=True)
    server._reader.start()

    deadline = time.time() + 90
    while time.time() < deadline:
        if server.process.poll() is not None:
            raise RuntimeError(
                f"实例 :{port} 启动即退出（exit={server.process.returncode}）：\n"
                + "\n".join(server.logs[-40:])
            )
        try:
            resp = httpx.get(f"{server.base_url}/health", timeout=2.0, trust_env=False)
            if resp.status_code == 200:
                print(f"[conftest] 已拉起实例 :{port}")
                return server
        except Exception:  # noqa: BLE001
            time.sleep(0.5)

    server.stop()
    raise RuntimeError(f"实例 :{port} 90 秒内没有就绪：\n" + "\n".join(server.logs[-40:]))


# ======================================================================
# 会话级装置
# ======================================================================
@pytest.fixture(scope="session")
def mysql_ready() -> tuple[bool, str]:
    return database_available()


@pytest.fixture(scope="session", autouse=True)
def report_test_data_footprint(mysql_ready: tuple[bool, str]):
    """会话结束时报告这次跑了多少测试数据（避免库悄悄膨胀）。

    只在 MySQL 可用时统计；不可用时什么都不做（测试本来就被 skip 了）。
    """
    ok, _ = mysql_ready
    if not ok:
        yield
        return

    try:
        before = _test_artifact_counts()
    except Exception:  # noqa: BLE001 —— 统计失败不该影响测试结果
        before = None

    yield

    if before is None:
        return
    try:
        after = _test_artifact_counts()
    except Exception:  # noqa: BLE001
        return

    delta = {key: after[key] - before.get(key, 0) for key in after}
    if any(value > 0 for value in delta.values()):
        print(
            "\n[conftest] 本次测试在库里新增："
            + "，".join(f"{key}={value}" for key, value in delta.items() if value > 0)
        )
        print("[conftest] 想清理：uv run python scripts/cleanup_test_data.py")


def _test_artifact_counts() -> dict[str, int]:
    """测试数据的"足迹"（都以 pytest_* 账号为锚点）。"""
    users = query_one(
        "SELECT COUNT(1) AS n FROM users WHERE username LIKE 'pytest\\_%'"
    )
    return {"测试账号": int(users["n"]) if users else 0}


@pytest.fixture(scope="session")
def db_or_skip(mysql_ready: tuple[bool, str]) -> None:
    ok, why = mysql_ready
    if not ok:
        pytest.skip(f"MySQL 不可用，跳过集成测试（{why}）")


@pytest.fixture(scope="session")
def business_server(db_or_skip: None) -> Server:
    """普通实例：业务 / 并发 / 权限 / 知识库测试打它。"""
    server = _start_server(BUSINESS_PORT, {"APP_ENV": "test"})
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def agent_server(db_or_skip: None) -> Server:
    """确定性 LLM 桩实例（不动业务）：Agent 重试 / 断点续跑 / 互斥 / HITL 测试打它。

    桩只给结论（`business_action` 为空），所以这些测试**不会真的动钱** ——
    并发与恢复测试关心的是调度，不该顺手退掉演示数据里的订单。
    要验"Agent 真的退款"请用下面的 chain_server。
    """
    server = _start_server(
        AGENT_PORT,
        {
            "APP_ENV": "test",
            "AGENT_STUB_LLM": "1",
            "AGENT_STUB_DELAY_SECONDS": STUB_DELAY,
        },
    )
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def chain_server(db_or_skip: None) -> Server:
    """确定性 LLM 桩实例（**会真的动业务**）：端到端链路测试打它。

    唯一差别是 `AGENT_STUB_BUSINESS_ACTION=apply_refund` —— 于是 Agent 的最终决策会
    真的走 Tool → RefundService → Repository → MySQL 把订单退掉。

    为什么必须单独一个实例：桩的 business_action 是**启动时一次性读入**的
    （真实模型的行为也不可能在运行中按工单切换），所以"动不动钱"这件事
    只能在实例级别分开，才能保证两组测试互不污染。
    """
    server = _start_server(
        CHAIN_PORT,
        {
            "APP_ENV": "test",
            "AGENT_STUB_LLM": "1",
            "AGENT_STUB_DELAY_SECONDS": "0.2",
            "AGENT_STUB_BUSINESS_ACTION": "apply_refund",
        },
    )
    try:
        yield server
    finally:
        server.stop()


# ======================================================================
# HTTP 客户端
# ======================================================================
class Api:
    """一个已登录用户/客服的 HTTP 会话（每个实例一个，线程安全靠"每线程新建 client"）。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.api = f"{base_url}/api"
        self.token: str | None = None
        self.username: str | None = None
        self.role: str | None = None
        self.client = httpx.Client(base_url=self.api, timeout=180.0, trust_env=False)

    # ---- 登录 ----
    def login(self, username: str, password: str) -> dict:
        resp = self.client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text[:200]}"
        payload = resp.json()
        self.token = payload["access_token"]
        self.username = username
        self.role = payload["user"]["role"]
        return payload["user"]

    def register_and_login(self, prefix: str) -> dict:
        username = f"{prefix}_{uuid.uuid4().hex[:8]}"
        resp = self.client.post(
            "/auth/register",
            json={"username": username, "password": USER_PASSWORD, "nickname": username},
        )
        assert resp.status_code in (200, 201), f"注册失败：{resp.status_code} {resp.text[:200]}"
        return self.login(username, USER_PASSWORD)

    # ---- 请求 ----
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        headers.update(self.headers())
        return self.client.request(method, path, headers=headers, **kw)

    def ok(self, method: str, path: str, **kw):
        resp = self.call(method, path, **kw)
        assert 200 <= resp.status_code < 300, (
            f"{method} {path} → HTTP {resp.status_code}: {resp.text[:300]}"
        )
        return resp.json()

    def status(self, method: str, path: str, **kw) -> int:
        return self.call(method, path, **kw).status_code

    def code(self, method: str, path: str, **kw) -> str | None:
        """错误响应里的业务码（BusinessError.code）。"""
        try:
            return self.call(method, path, **kw).json().get("code")
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        self.client.close()


def concurrent(method: str, base_url: str, path: str, token: str | None, count: int, *, json_body=None) -> list[httpx.Response]:
    """用 Barrier 对齐起跑线，同时发 count 个请求。

    为什么每个线程一个 httpx.Client：Client 不是线程安全的共享对象，
    共用会让"并发"变成"串行 + 偶尔炸"。
    """
    barrier = threading.Barrier(count)
    responses: list[httpx.Response | None] = [None] * count

    def one(index: int) -> None:
        with httpx.Client(base_url=f"{base_url}/api", timeout=180.0, trust_env=False) as raw:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            barrier.wait()
            responses[index] = raw.request(method, path, headers=headers, json=json_body)

    threads = [threading.Thread(target=one, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [resp for resp in responses if resp is not None]


# ======================================================================
# 业务装置（每个测试一个全新账号，互不干扰）
# ======================================================================
@pytest.fixture
def user(business_server: Server) -> Api:
    api = Api(business_server.base_url)
    api.register_and_login("pytest_u")
    yield api
    api.close()


@pytest.fixture
def other_user(business_server: Server) -> Api:
    """第二个用户：专门用来验"跨用户数据隔离 / 越权"。"""
    api = Api(business_server.base_url)
    api.register_and_login("pytest_other")
    yield api
    api.close()


@pytest.fixture
def staff(business_server: Server) -> Api:
    api = Api(business_server.base_url)
    api.login(**CS_LOGIN)
    yield api
    api.close()


def register_staff(server: Server, prefix: str = "pytest_cs") -> Api:
    """注册一个客服账号并登录。

    为什么需要它：演示数据只有一个 `service01`，而"两个客服同时抢同一张工单"
    这类并发用例必须是**两个不同的操作者** —— 同一个 token 发两次请求，
    业务上就是"同一个人连点两次"，本来就该幂等成功（那是正确行为，不是竞态）。

    角色直接写库（没有公开的"注册客服"接口，也不该有 —— 那会是个提权漏洞）。
    """
    api = Api(server.base_url)
    user = api.register_and_login(prefix)
    execute("UPDATE users SET role='CUSTOMER_SERVICE' WHERE id=%s", (user["user_id"],))
    # 重新登录，让 JWT 里带上新角色（角色写在 token 里）
    api.login(api.username, USER_PASSWORD)
    assert api.role == "CUSTOMER_SERVICE", f"客服角色没有生效：{api.role}"
    return api


@pytest.fixture
def admin(business_server: Server) -> Api:
    api = Api(business_server.base_url)
    api.login(**ADMIN_LOGIN)
    yield api
    api.close()


# ======================================================================
# 业务动作小工具
# ======================================================================
def create_product(admin: Api, *, price: float = 100.0, stock: int = 10) -> dict:
    return admin.ok(
        "POST",
        "/admin/products",
        json={
            "name": f"pytest商品_{uuid.uuid4().hex[:6]}",
            "price": price,
            "stock": stock,
            "description": "pytest",
        },
    )


def buy(user: Api, product_id: int, *, quantity: int = 1) -> dict:
    """加购 → 下单，返回订单详情。"""
    user.ok("POST", "/cart/items", json={"product_id": product_id, "quantity": quantity})
    return user.ok("POST", "/orders", json={})


def create_after_sale(user: Api, order_id: int, *, reason: str = "pytest 售后") -> dict:
    return user.ok(
        "POST", "/after-sales", json={"order_id": order_id, "type": "REFUND", "reason": reason}
    )


def create_ticket(user: Api, *, content: str, order_id: int | None = None, type: str | None = None) -> int:
    body: dict = {"content": content}
    if order_id is not None:
        body["order_id"] = order_id
    if type is not None:
        body["type"] = type
    return user.ok("POST", "/tickets", json=body)["ticket_id"]


# ======================================================================
# Agent 实例专用：登记桩行为 / 故障注入
# ======================================================================
def stub_registry(server: Server, kind: str, ticket_id: int) -> dict:
    """把工单登记进 LLM 桩的名单（human / fail / fatal）。"""
    resp = httpx.post(
        f"{server.api}/__test__/stub-tickets",
        json={"kind": kind, "ticket_id": ticket_id},
        timeout=30.0,
        trust_env=False,
    )
    assert resp.status_code == 200, resp.text[:200]
    return resp.json()


def fail_node_once(server: Server, node: str, ticket_id: int, *, message: str | None = None) -> dict:
    body: dict = {"node": node, "ticket_id": ticket_id}
    if message:
        body["message"] = message
    resp = httpx.post(
        f"{server.api}/__test__/fail-node-once", json=body, timeout=30.0, trust_env=False
    )
    assert resp.status_code == 200, resp.text[:200]
    return resp.json()


def restore_nodes(server: Server) -> None:
    httpx.post(f"{server.api}/__test__/restore-nodes", timeout=30.0, trust_env=False)


# ======================================================================
# Agent 数据库断言
# ======================================================================
def ticket_row(ticket_id: int) -> dict:
    row = query_one(
        "SELECT id, status, agent_status, agent_run_id, assignee_id, user_id "
        "FROM tickets WHERE id=%s",
        (ticket_id,),
    )
    assert row is not None, f"工单 {ticket_id} 不存在"
    return row


def runs_of(ticket_id: int) -> list[dict]:
    return query_all(
        "SELECT id, status, error, retry_count, resumed, current_node, thread_id, "
        "need_human, used_rag, used_tool FROM agent_runs WHERE ticket_id=%s ORDER BY id",
        (ticket_id,),
    )


def active_runs(ticket_id: int) -> list[dict]:
    """活跃执行（还没结束的那些）—— 并发互斥的核心断言对象。"""
    return [
        run
        for run in runs_of(ticket_id)
        if run["status"] in ("PENDING", "RUNNING", "WAITING_HUMAN")
    ]


def run_event_messages(run_ids: list[int], keyword: str | None = None) -> list[str]:
    if not run_ids:
        return []
    placeholders = ",".join(["%s"] * len(run_ids))
    if keyword is None:
        rows = query_all(
            f"SELECT message FROM agent_run_events WHERE agent_run_id IN ({placeholders}) ORDER BY id",
            tuple(run_ids),
        )
    else:
        rows = query_all(
            f"SELECT message FROM agent_run_events WHERE agent_run_id IN ({placeholders}) "
            "AND message LIKE %s ORDER BY id",
            (*run_ids, f"%{keyword}%"),
        )
    return [row["message"] for row in rows]


def refund_count(order_id: int, *, status: str | None = None) -> int:
    if status:
        row = query_one(
            "SELECT COUNT(1) AS n FROM refund_records WHERE order_id=%s AND status=%s",
            (order_id, status),
        )
    else:
        row = query_one("SELECT COUNT(1) AS n FROM refund_records WHERE order_id=%s", (order_id,))
    return int(row["n"]) if row else 0


def payment_count(order_id: int, *, status: str | None = None) -> int:
    if status:
        row = query_one(
            "SELECT COUNT(1) AS n FROM payments WHERE order_id=%s AND status=%s",
            (order_id, status),
        )
    else:
        row = query_one("SELECT COUNT(1) AS n FROM payments WHERE order_id=%s", (order_id,))
    return int(row["n"]) if row else 0


def order_row(order_id: int) -> dict:
    row = query_one(
        "SELECT id, order_no, payment_status, fulfillment_status, after_sale_status "
        "FROM orders WHERE id=%s",
        (order_id,),
    )
    assert row is not None, f"订单 {order_id} 不存在"
    return row


def product_stock(product_id: int) -> int:
    row = query_one("SELECT stock FROM products WHERE id=%s", (product_id,))
    assert row is not None
    return int(row["stock"])
