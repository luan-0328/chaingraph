"""并发控制验收：**同工单互斥、不同工单并发**（一条命令跑完全部测试）。

    cd backend
    uv run python scripts/concurrency_check.py            # 全部测试
    uv run python scripts/concurrency_check.py --only 5   # 只跑"并发竞争"
    uv run python scripts/concurrency_check.py --keep     # 保留测试实例（调试用）

它和 scripts/acceptance.py 的分工
--------------------------------
acceptance.py 管"业务链路对不对"（真实 LLM、场景 A~D）；
本脚本管"**并发控制对不对**"，所以它不测业务，只测那几条关键不变量：

    同一工单：任意并发数量的请求 → 有且只有一个 AgentRun
    不同工单：多个 AgentRun 同时在跑（总耗时 ≈ 单次耗时，而不是相加）
    中途失败：能从 Checkpoint 断点续跑，已完成的节点不重跑

为此它自己拉起一个**带确定性 LLM 桩的实例**（:8010，AGENT_STUB_LLM=1）：
桩把每次"模型调用"固定成 sleep N 秒，于是"并发"这件事变得可测量 ——
真实 LLM 的耗时抖动会让并发窗口时有时无，那样的测试说明不了问题。
被测的其它部分（MySQL 抢占、finally 释放、心跳、HITL 恢复原图、断点续跑、落库、SSE）
全是真实代码路径。

覆盖的测试（对应设计文档的测试一~测试五 + 附加项）：
    测试一  同一工单 10 并发请求       → 只创建 1 个 AgentRun
    测试二  10 个不同工单并发          → 10 个 AgentRun，且真的并行（看耗时）
    测试三  Agent 抛异常               → run=FAILED、agent_status=IDLE（不永久卡死）
    测试四  WAITING_HUMAN 再点处理     → 不新建 run，返回 WAITING_HUMAN；批准后恢复原 run
    测试五  并发竞争（10 请求同工单）   → 数据库里只有一个活跃 run（最核心的验收标准）
    测试六  断点恢复                   → 中途失败后从 Checkpoint 续跑，已完成的节点不重跑
    附加    可恢复异常自动重试、不可恢复异常不重试
    附加    僵尸执行回收（进程崩溃留下的 RUNNING 能被接管）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.agent_run import AgentRunStatus  # noqa: E402  （必须先插好 sys.path）

HOST = "127.0.0.1"
PORT = 8010
BASE = f"http://{HOST}:{PORT}"
API = f"{BASE}/api"
HEALTH = f"{BASE}/health"

# 桩的配置：delay=1.2s → 一次执行 ≈ 2.4s（analyze + result 各一次模型调用）。
# HUMAN/FAIL/FATAL 的工单号由测试在拿到 ticket_id 之后**动态登记**（见 _patch_stub）。
STUB_DELAY = "1.2"

_passed: list[str] = []
_failed: list[str] = []
_skipped: list[str] = []


# ======================================================================
# 输出与断言
# ======================================================================
def section(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    (_passed if ok else _failed).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    _skipped.append(name)
    print(f"  ○ SKIP {name}  [{why}]")


def note(text: str) -> None:
    print(f"      · {text}")


# ======================================================================
# HTTP / 数据库小工具
# ======================================================================
class Api:
    def __init__(self, username: str | None = None) -> None:
        self.client = httpx.Client(base_url=API, timeout=180.0, trust_env=False)
        self.token: str | None = None
        self.username = username

    def login(self, username: str, password: str) -> dict:
        resp = self.client.post("/auth/login", json={"username": username, "password": password})
        resp.raise_for_status()
        payload = resp.json()
        self.token = payload["access_token"]
        self.username = username
        return payload["user"]

    def register_and_login(self, prefix: str) -> None:
        username = f"{prefix}_{uuid.uuid4().hex[:8]}"
        self.client.post(
            "/auth/register",
            json={"username": username, "password": "123456", "nickname": username},
        ).raise_for_status()
        self.login(username, "123456")

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.client.request(method, path, headers=headers, **kw)

    def ok(self, method: str, path: str, **kw):
        resp = self.call(method, path, **kw)
        if not 200 <= resp.status_code < 300:
            raise AssertionError(f"{method} {path} → HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def db_connect():
    import pymysql
    from urllib.parse import urlparse

    from app.core.config import settings

    url = urlparse(settings.database_url.replace("mysql+pymysql://", "mysql://"))
    return pymysql.connect(
        host=url.hostname,
        port=url.port or 3306,
        user=url.username,
        password=url.password,
        database=(url.path or "/").lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def ticket_row(ticket_id: int) -> dict | None:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, agent_status, agent_run_id FROM tickets WHERE id=%s",
                (ticket_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def runs_of(ticket_id: int) -> list[dict]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, error, retry_count, resumed, started_at, alive_at, thread_id "
                "FROM agent_runs WHERE ticket_id=%s ORDER BY id",
                (ticket_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def active_run_count(ticket_id: int) -> int:
    """数据库里的"活跃 AgentRun"数量（PENDING/RUNNING/WAITING_HUMAN）—— 核心验收指标。

    注意是 `RUNNING` 而不是 `PROCESSING`：AgentRunStatus 早期叫 PROCESSING，
    后来改名成 RUNNING（和工单的 PROCESSING 区分开，见 models/agent_run.py），
    老值由 core/schema.py 在启动时回填。这里漏改会让"活跃执行数"永远算成 0 ——
    而 0 恰好能让"只创建了一个 AgentRun"之类的断言**假通过**，
    所以这个字面量必须和 AgentRunStatus 保持一致。
    """
    statuses = ",".join(f"'{status}'" for status in AgentRunStatus.ACTIVE)
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(1) AS n FROM agent_runs WHERE ticket_id=%s AND status IN ({statuses})",
                (ticket_id,),
            )
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def run_event_messages(run_ids: list[int], keyword: str) -> list[str]:
    """跨多条 AgentRun 捞出包含关键字的业务事件（用来数"某个节点跑了几次"）。"""
    if not run_ids:
        return []
    conn = db_connect()
    try:
        placeholders = ",".join(["%s"] * len(run_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT message FROM agent_run_events WHERE agent_run_id IN ({placeholders}) "
                "AND message LIKE %s ORDER BY id",
                (*run_ids, f"%{keyword}%"),
            )
            return [row["message"] for row in cur.fetchall()]
    finally:
        conn.close()


def create_ticket(api: Api, content: str = "并发测试工单") -> int:
    payload = api.ok("POST", "/tickets", json={"content": content})
    return payload["ticket_id"]


def process_concurrently(api: Api, ticket_id: int, count: int, *, timeout: float = 180.0) -> list[dict]:
    """同时（用 Barrier 对齐起跑线）发 count 个 /process 请求，返回每个响应的 JSON。"""
    barrier = threading.Barrier(count)
    results: list[dict | None] = [None] * count
    statuses: list[int] = [0] * count

    def one(index: int) -> None:
        # 每个请求用独立 client：httpx.Client 不是线程安全的共享对象
        with httpx.Client(base_url=API, timeout=timeout, trust_env=False) as raw:
            headers = {"Authorization": f"Bearer {api.token}"}
            barrier.wait()
            resp = raw.post(f"/tickets/{ticket_id}/process", headers=headers)
            statuses[index] = resp.status_code
            try:
                results[index] = resp.json()
            except Exception:  # noqa: BLE001
                results[index] = {"raw": resp.text}

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(one, range(count)))

    for index, status in enumerate(statuses):
        if status >= 400:
            results[index] = {**(results[index] or {}), "http_status": status}
    return [item or {} for item in results]


def process_many_tickets(api: Api, ticket_ids: list[int]) -> tuple[list[dict], float]:
    """同时处理多张**不同**工单，返回 (结果列表, 墙钟耗时秒)。"""
    barrier = threading.Barrier(len(ticket_ids))
    results: list[dict | None] = [None] * len(ticket_ids)

    def one(index: int) -> None:
        with httpx.Client(base_url=API, timeout=180.0, trust_env=False) as raw:
            headers = {"Authorization": f"Bearer {api.token}"}
            barrier.wait()
            started = time.perf_counter()
            resp = raw.post(f"/tickets/{ticket_ids[index]}/process", headers=headers)
            results[index] = {
                "elapsed": round(time.perf_counter() - started, 2),
                "http_status": resp.status_code,
                "payload": resp.json() if resp.status_code < 500 else {},
            }

    started_all = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(ticket_ids)) as pool:
        list(pool.map(one, range(len(ticket_ids))))
    return [item or {} for item in results], round(time.perf_counter() - started_all, 2)


# ======================================================================
# 测试实例（确定性 LLM 桩）
# ======================================================================
def _reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3.0, trust_env=False).status_code < 500
    except Exception:  # noqa: BLE001
        return False


def ensure_test_instance() -> tuple[bool, object | None]:
    """拉起带 LLM 桩的测试实例；端口已被占用时复用（保证桩配置一致）。"""
    if _reachable(HEALTH):
        try:
            health = httpx.get(HEALTH, timeout=5.0, trust_env=False).json()
        except Exception:  # noqa: BLE001
            health = {}
        if health.get("agent_stub_llm"):
            print(f"  （复用已在运行的并发测试实例 :{PORT}）")
            return True, None
        print(f"  ✗ :{PORT} 已被占用，但它不是并发测试实例（agent_stub_llm=false），请先停掉它")
        return False, None

    env = {
        **os.environ,
        "AGENT_STUB_LLM": "1",
        "AGENT_STUB_DELAY_SECONDS": STUB_DELAY,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,  # 不用管道：避免和沙箱/管道限制纠缠
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):  # 最多等 40 秒
        if _reachable(HEALTH):
            print(f"  （已拉起并发测试实例 :{PORT}，LLM 桩 delay={STUB_DELAY}s）")
            return True, proc
        time.sleep(0.5)
    proc.terminate()
    return False, None


# ======================================================================
# 测试一：同一工单并发（10 个请求 → 1 个 AgentRun）
# ======================================================================
def test_same_ticket(api: Api) -> None:
    section("测试一：同一工单 10 个并发请求 → 只创建 1 个 AgentRun")
    ticket_id = create_ticket(api, "同一工单并发测试")
    note(f"工单 id={ticket_id}")

    results = process_concurrently(api, ticket_id, 10)
    runs = runs_of(ticket_id)
    row = ticket_row(ticket_id)

    new_runs = [r for r in results if not r.get("used_existing_run")]
    reused = [r for r in results if r.get("used_existing_run")]

    check("总共有且只有 1 个 AgentRun 被创建", len(runs) == 1, f"agent_runs 行数={len(runs)}")
    check(
        "只有 1 个请求真的执行了（其余 9 个返回已有 Run）",
        len(new_runs) == 1 and len(reused) == 9,
        f"执行={len(new_runs)} 复用={len(reused)}",
    )
    check(
        "复用的响应 status=ALREADY_RUNNING 且指向同一个 run",
        all(r.get("status") == "ALREADY_RUNNING" for r in reused)
        and len({r.get("agent_run_id") for r in reused}) == 1,
        f"状态={sorted({r.get('status') for r in reused})} run={sorted({r.get('agent_run_id') for r in reused})}",
    )
    check(
        "所有请求返回的都是同一个 agent_run_id",
        len({r.get("agent_run_id") for r in results}) == 1,
        str(sorted({r.get("agent_run_id") for r in results})),
    )
    check("执行结束后 agent_status 回到 IDLE", row["agent_status"] == "IDLE", str(row["agent_status"]))
    check("数据库里只剩 0 个活跃 run（执行已结束）", active_run_count(ticket_id) == 0, str(active_run_count(ticket_id)))


# ======================================================================
# 测试二：不同工单并发（10 个工单 → 10 个 AgentRun 同时跑）
# ======================================================================
def test_different_tickets(api: Api) -> None:
    section("测试二：10 张不同工单并发 → 10 个 AgentRun，且真的并行")

    ticket_ids = [create_ticket(api, f"不同工单并发测试 #{i}") for i in range(10)]
    note(f"工单：{ticket_ids}")

    results, wall = process_many_tickets(api, ticket_ids)
    expected_serial = 2 * float(STUB_DELAY) * len(ticket_ids)

    ok_status = [r.get("http_status") == 200 for r in results]
    statuses = [r.get("payload", {}).get("status") for r in results]
    run_ids = {r.get("payload", {}).get("agent_run_id") for r in results}
    max_elapsed = max((r.get("elapsed") or 0) for r in results)

    check("10 个请求全部成功", all(ok_status), f"HTTP={[r.get('http_status') for r in results]}")
    check(
        "每个工单各创建了自己的 AgentRun（10 个不同的 run）",
        len(run_ids) == 10 and all(rid for rid in run_ids),
        f"run_ids={sorted(x for x in run_ids if x)}",
    )
    check(
        "全部执行完成（COMPLETED）",
        all(status == "COMPLETED" for status in statuses),
        str(sorted(set(statuses))),
    )
    total_runs = sum(len(runs_of(tid)) for tid in ticket_ids)
    check("每张工单都恰好 1 个 AgentRun", total_runs == 10, f"合计={total_runs}")
    check(
        "真的并行：总耗时远小于串行（不是被全局锁串起来）",
        wall < expected_serial * 0.5,
        f"实际 {wall}s（单次≈{2 * float(STUB_DELAY)}s，若串行≈{expected_serial}s，最慢单请求 {max_elapsed}s）",
    )


# ======================================================================
# 测试三：Agent 异常 → FAILED + IDLE（不永久卡死）
# ======================================================================
def test_agent_failure_does_not_stick(api: Api) -> None:
    section("测试三：Agent 抛异常 → AgentRun=FAILED、agent_status=IDLE（不永久卡 RUNNING）")
    ticket_id = create_ticket(api, "Agent 异常测试")
    _patch_stub("fatal", ticket_id)
    note(f"桩配置：工单 {ticket_id} 的模型调用直接抛不可恢复异常")

    resp = api.call("POST", f"/tickets/{ticket_id}/process")
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    check(
        "接口返回可读的业务错误（不是裸 500 崩溃）",
        resp.status_code >= 400 and bool(payload.get("code") or payload.get("message")),
        f"HTTP {resp.status_code} code={payload.get('code')}",
    )

    row = ticket_row(ticket_id)
    runs = runs_of(ticket_id)
    check("工单不再处于 agent_status=RUNNING（执行权已释放）", row["agent_status"] == "IDLE", str(row["agent_status"]))
    check("工单业务状态不是 PROCESSING（用户不会被永久晾着）", row["status"] != "PROCESSING", str(row["status"]))
    check(
        "AgentRun 落库为 FAILED 且有错误原因",
        bool(runs) and runs[-1]["status"] == "FAILED" and bool(runs[-1]["error"]),
        f"{runs[-1]['status'] if runs else '-'} / {(runs[-1]['error'] or '')[:60] if runs else '-'}",
    )

    # 失败之后应当允许重新发起（新的 AgentRun）
    again = api.call("POST", f"/tickets/{ticket_id}/process")
    check(
        "失败后可以重新发起处理（会新建一次 AgentRun）",
        again.status_code == 200 and len(runs_of(ticket_id)) >= 2,
        f"HTTP {again.status_code} runs={len(runs_of(ticket_id))}",
    )


# ======================================================================
# 测试四：WAITING_HUMAN（不新建 run；批准后恢复原 run）
# ======================================================================
def test_waiting_human(api: Api, cs: Api) -> None:
    section("测试四：WAITING_HUMAN —— 再点处理不新建 run，客服批准后恢复原 run")
    ticket_id = create_ticket(api, "HITL 并发测试")
    _patch_stub("human", ticket_id)
    note(f"工单 id={ticket_id} 已登记进桩的 HITL 名单")

    first = api.ok("POST", f"/tickets/{ticket_id}/process")
    check("Agent 停在人工确认点", first["status"] == "WAITING_HUMAN" and first["need_human"] is True, str(first["status"]))
    run_id = first["agent_run_id"]
    row = ticket_row(ticket_id)
    check("工单 agent_status=WAITING_HUMAN", row["agent_status"] == "WAITING_HUMAN", str(row["agent_status"]))

    # 用户再次点击"处理"
    again = api.call("POST", f"/tickets/{ticket_id}/process")
    payload = again.json() if again.status_code == 200 else {}
    check(
        "再次点处理：不新建 AgentRun，返回 WAITING_HUMAN + 原 run_id",
        again.status_code == 200
        and payload.get("status") == "WAITING_HUMAN"
        and payload.get("agent_run_id") == run_id
        and payload.get("used_existing_run") is True,
        f"HTTP {again.status_code} status={payload.get('status')} run={payload.get('agent_run_id')} "
        f"reused={payload.get('used_existing_run')}",
    )
    check("始终只有 1 个 AgentRun", len(runs_of(ticket_id)) == 1, f"runs={len(runs_of(ticket_id))}")

    # 客服批准 → 恢复原图
    approved = cs.ok("POST", f"/tickets/{ticket_id}/approve", json={"comment": "并发测试：同意"})
    runs = runs_of(ticket_id)
    check(
        "批准后仍然是同一个 AgentRun（恢复原执行，不是新建）",
        len(runs) == 1 and runs[0]["id"] == run_id,
        f"runs={[r['id'] for r in runs]}",
    )
    check("该 run 已推进到终态 COMPLETED", runs[0]["status"] == "COMPLETED", str(runs[0]["status"]))
    check(
        "批准后执行权释放（agent_status=IDLE）",
        ticket_row(ticket_id)["agent_status"] == "IDLE",
        str(ticket_row(ticket_id)["agent_status"]),
    )
    note(f"客服批准返回的工单状态：{approved.get('status')}")


# ======================================================================
# 测试五：并发竞争（最核心的验收标准）
# ======================================================================
def test_race_same_ticket(api: Api) -> None:
    section("测试五：并发竞争 —— 10 个请求同时打同一张工单")
    ticket_id = create_ticket(api, "并发竞争测试（核心验收项）")
    note(f"工单 id={ticket_id}，10 个请求用 Barrier 对齐起跑线后同时发出")

    results = process_concurrently(api, ticket_id, 10)
    runs = runs_of(ticket_id)
    row = ticket_row(ticket_id)

    executed = [r for r in results if not r.get("used_existing_run")]
    reused = [r for r in results if r.get("used_existing_run")]
    errors = [r for r in results if r.get("http_status", 0) >= 400]

    print("      每个请求的结果：")
    for index, item in enumerate(results):
        print(
            f"        [{index:02d}] status={item.get('status')!s:<16} run={item.get('agent_run_id')!s:<6} "
            f"reused={item.get('used_existing_run')}"
        )

    check("没有任何请求报错", not errors, json.dumps(errors[:2], ensure_ascii=False)[:160])
    check("数据库里只出现一个 AgentRun", len(runs) == 1, f"agent_runs={[r['id'] for r in runs]}")
    check("恰好 1 个请求真的执行了", len(executed) == 1, f"执行={len(executed)} 复用={len(reused)}")
    check("最终 agent_status 不是 RUNNING（执行权已正确归还）", row["agent_status"] == "IDLE", str(row["agent_status"]))
    check("工单上记录的活跃 run 指针已清空", row["agent_run_id"] is None, str(row["agent_run_id"]))


# ======================================================================
# 测试六：断点恢复（图跑到一半失败 → 从 Checkpoint 续跑）
# ======================================================================
def _fail_node_once(node: str, ticket_id: int) -> bool:
    """让某个图节点对目标工单**首次调用**失败（验证断点续跑用）。"""
    try:
        resp = httpx.post(
            f"{API}/__test__/fail-node-once",
            json={"node": node, "ticket_id": ticket_id},
            timeout=10.0,
            trust_env=False,
        )
    except Exception as exc:  # noqa: BLE001
        check(f"注入节点故障 {node}", False, f"请求异常：{type(exc).__name__}: {exc}")
        return False
    check(
        f"注入节点故障 {node}@ticket={ticket_id}",
        resp.status_code == 200,
        f"HTTP {resp.status_code} {resp.text[:120]}",
    )
    return resp.status_code == 200


def _restore_nodes() -> None:
    try:
        httpx.post(f"{API}/__test__/restore-nodes", timeout=10.0, trust_env=False)
    except Exception:  # noqa: BLE001
        pass


def test_resume_from_checkpoint(api: Api, *, node: str = "retrieve_knowledge") -> None:
    """测试六：图跑到一半失败 → 从断点续跑，已完成的节点不重跑。

    现场构造：让指定只读节点对目标工单首次调用就抛"可恢复异常"。以 `retrieve_knowledge`
    为例：
        第 1 轮：analyze 成功 → retrieve 抛异常 → 图中断，Checkpoint 停在 retrieve 之前
        第 2 轮：`ainvoke(None, ...)` 从 retrieve 继续 —— 而不是从头把 analyze 再跑一遍
    可观测的差别：**「正在分析工单」事件只出现一次，且排在「从断点续跑」之前**。
    另外 thread_id 上有唯一约束，所以续跑必须复用同一条 AgentRun —— 也在这里一起断言。
    """
    section(f"测试六：断点恢复（{node}）—— 中途失败后从 Checkpoint 续跑，已完成的节点不重跑")
    ticket_id = create_ticket(api, f"断点恢复测试（{node}）")
    if not _fail_node_once(node, ticket_id):
        return

    try:
        started = time.perf_counter()
        resp = api.call("POST", f"/tickets/{ticket_id}/process")
        payload = resp.json() if resp.status_code == 200 else {}
        elapsed = round(time.perf_counter() - started, 2)

        rows = runs_of(ticket_id)
        run_ids = [row["id"] for row in rows]

        check(
            "整体成功（失败的那一步被续跑补上了）",
            resp.status_code == 200 and payload.get("status") == "COMPLETED",
            f"HTTP {resp.status_code} status={payload.get('status')} 耗时={elapsed}s",
        )
        check(
            "只有一条 AgentRun（重试是同一条执行的继续，不是新的一次执行）",
            len(rows) == 1,
            f"runs={[(r['id'], r['status']) for r in rows]}",
        )
        if len(rows) == 1:
            run = rows[0]
            check("这条执行最终是 COMPLETED", run["status"] == "COMPLETED", str(run["status"]))
            check("retry_count 如实记录了重试次数", int(run["retry_count"] or 0) >= 1, str(run["retry_count"]))
            check("resumed 标记为真（说明用的是断点续跑，不是从头重跑）", bool(run["resumed"]), str(run["resumed"]))
            check(
                "续跑成功后清掉了上一次尝试的 error（失败详情仍留在事件流里）",
                not run["error"],
                str(run["error"])[:60],
            )
        else:
            skip("断点续跑的单条 run 断言", "run 数量不为 1")

        ordered = _run_event_sequence(run_ids)
        check("过程流里有「从断点续跑」事件", any("从断点续跑" in msg for msg in ordered), f"{len(ordered)} 条事件")
        check(
            "已完成的 analyze 节点没有被重跑（「正在分析工单」只出现一次）",
            sum(1 for msg in ordered if "正在分析工单" in msg) == 1,
            f"出现 {sum(1 for msg in ordered if '正在分析工单' in msg)} 次（从头重跑会是 2）",
        )
        check(
            "顺序正确：先分析 → 再从断点续跑（证明是接着跑而不是重来）",
            _index_of(ordered, "正在分析工单") < _index_of(ordered, "从断点续跑"),
            str(ordered[:6]),
        )
        check(
            "最终执行权释放（agent_status=IDLE）",
            ticket_row(ticket_id)["agent_status"] == "IDLE",
            str(ticket_row(ticket_id)["agent_status"]),
        )
    finally:
        _restore_nodes()


def _run_event_sequence(run_ids: list[int]) -> list[str]:
    """按落库顺序取回事件文案（用来断言"哪个节点先跑、跑了几个"）。"""
    if not run_ids:
        return []
    conn = db_connect()
    try:
        placeholders = ",".join(["%s"] * len(run_ids))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT message FROM agent_run_events WHERE agent_run_id IN ({placeholders}) ORDER BY id",
                tuple(run_ids),
            )
            return [row["message"] for row in cur.fetchall()]
    finally:
        conn.close()


def _index_of(messages: list[str], keyword: str) -> int:
    """第一次出现关键字的序号；没出现返回一个很大的数（让比较断言失败）。"""
    for index, message in enumerate(messages):
        if keyword in message:
            return index
    return 10**6


# ======================================================================
# 附加：重试策略（可恢复异常重试、不可恢复不重试）
# ======================================================================
def test_retry_policy(api: Api) -> None:
    section("附加：重试策略 —— 可恢复异常重试，不可恢复异常不重试")
    # 桩的故障注入按"该工单的第 1 次模型调用"生效，所以两张工单必须各自新建
    retry_ticket = create_ticket(api, "可恢复异常重试测试")
    _patch_stub("fail", retry_ticket)
    note(f"桩配置：工单 {retry_ticket} 的第一次模型调用抛 timeout（可恢复）")

    started = time.perf_counter()
    resp = api.call("POST", f"/tickets/{retry_ticket}/process")
    elapsed = round(time.perf_counter() - started, 2)
    payload = resp.json() if resp.status_code == 200 else {}
    runs = runs_of(retry_ticket)

    check(
        "重试后成功（第 2 次尝试跑完）",
        resp.status_code == 200 and payload.get("status") == "COMPLETED",
        f"HTTP {resp.status_code} status={payload.get('status')} 耗时={elapsed}s",
    )
    check(
        "仍然只有一条 AgentRun（重试是同一次执行的继续，不是新的一次执行）",
        len(runs) == 1,
        f"runs={[(r['id'], r['status']) for r in runs]}",
    )
    check(
        "retry_count 记下了这次重试",
        bool(runs) and int(runs[0]["retry_count"] or 0) >= 1,
        str(runs[0]["retry_count"] if runs else "-"),
    )
    events = _run_event_sequence([r["id"] for r in runs])
    check(
        "事件流里留下了失败原因（历史可追溯，不会因为重试成功就丢掉）",
        any("可恢复异常" in message and "timed out" in message.lower() for message in events),
        next((m[:70] for m in events if "可恢复异常" in m), "-"),
    )
    check(
        "工单最终 agent_status=IDLE",
        ticket_row(retry_ticket)["agent_status"] == "IDLE",
        str(ticket_row(retry_ticket)["agent_status"]),
    )

    # ---- 不可恢复异常：只尝试一次，不重试 ----
    fatal_ticket = create_ticket(api, "不可恢复异常测试")
    _patch_stub("fatal", fatal_ticket)
    resp = api.call("POST", f"/tickets/{fatal_ticket}/process")
    fatal_runs = runs_of(fatal_ticket)
    check(
        "不可恢复异常：只尝试 1 次就失败（不重试）",
        resp.status_code >= 400 and len(fatal_runs) == 1 and fatal_runs[0]["status"] == "FAILED",
        f"HTTP {resp.status_code} runs={[(r['id'], r['status']) for r in fatal_runs]}",
    )
    check(
        "失败后执行权照样释放（agent_status=IDLE）",
        ticket_row(fatal_ticket)["agent_status"] == "IDLE",
        str(ticket_row(fatal_ticket)["agent_status"]),
    )


# ======================================================================
# 附加：僵尸执行回收（进程崩溃留下的 RUNNING 不会永久卡死）
# ======================================================================
def test_zombie_execution(api: Api) -> None:
    """模拟"Agent 执行到一半进程被杀"：手工把工单改成 RUNNING + 老心跳，再点处理。

    正常流程测不出这个场景（finally 一定会释放），但它是真实会发生的：
    容器被 kill、机器断电、部署重启，都会留下 agent_status=RUNNING 而没人释放。
    这里直接用 SQL 复现那一刻的数据库状态，再看系统能否自愈。
    """
    section("附加：僵尸执行回收 —— 进程崩溃留下的 RUNNING 能被接管")
    ticket_id = create_ticket(api, "僵尸执行测试")
    _patch_stub("fatal", ticket_id)  # 让接管后的新执行立刻失败，测试更快也更确定

    # 制造一个"执行跑到一半"的现场。
    # 心跳要用 UTC 写：应用统一用 UTC（core/time.py），而 MySQL 的 NOW() 是服务器本地时间，
    # 直接拿来当心跳会得到"未来的心跳"，看起来永远新鲜。
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_runs (ticket_id, user_id, thread_id, status, started_at, alive_at, "
                "used_rag, used_tool, need_human, retry_count, created_at, updated_at) "
                "SELECT id, user_id, CONCAT('zombie-', id, '-', UNIX_TIMESTAMP()), 'RUNNING', "
                "UTC_TIMESTAMP() - INTERVAL 2 HOUR, UTC_TIMESTAMP() - INTERVAL 2 HOUR, 0, 0, 0, 0, "
                "UTC_TIMESTAMP(), UTC_TIMESTAMP() "
                "FROM tickets WHERE id=%s",
                (ticket_id,),
            )
            zombie_run_id = cur.lastrowid
            cur.execute(
                "UPDATE tickets SET agent_status='RUNNING', agent_run_id=%s, status='PROCESSING' WHERE id=%s",
                (zombie_run_id, ticket_id),
            )
        conn.commit()
    finally:
        conn.close()
    note(f"人为制造僵尸执行：ticket={ticket_id} run={zombie_run_id} 心跳停在 2 小时前")

    row = ticket_row(ticket_id)
    check("现场已就绪：工单卡在 agent_status=RUNNING", row["agent_status"] == "RUNNING", str(row["agent_status"]))

    # 用户点"开始处理"：系统应该识别出僵尸、回收执行权，并正常跑一次
    resp = api.call("POST", f"/tickets/{ticket_id}/process")
    payload = resp.json() if resp.status_code == 200 else {}
    rows = runs_of(ticket_id)
    final_row = ticket_row(ticket_id)

    check(
        "僵尸执行不再永久占锁：工单能被重新处理（执行权被回收后重抢成功）",
        len(rows) >= 2 and final_row["agent_status"] != "RUNNING",
        f"HTTP {resp.status_code} runs={[(r['id'], r['status']) for r in rows]} "
        f"agent_status={final_row['agent_status']}",
    )
    check(
        "僵尸 run 被标记为 FAILED（并写明原因）",
        any(r["id"] == zombie_run_id and r["status"] == "FAILED" for r in rows),
        str([(r["id"], r["status"]) for r in rows]),
    )
    check(
        "执行权最终回到 IDLE（没有被僵尸 run 的指针挂住）",
        final_row["agent_status"] == "IDLE",
        str(final_row["agent_status"]),
    )
    note(f"接管后的新执行结果：status={payload.get('status')}（桩把它配成不可恢复失败，属预期）")


# ======================================================================
# 让桩"认识"刚创建的工单
# ======================================================================
def _patch_stub(kind: str, ticket_id: int) -> None:
    """通过测试专用接口把工单号写进桩的行为名单（human / fail / fatal）。

    走不了（例如实例在别的进程且没有该接口）时直接报失败，
    绝不让对应断言因为"桩没配好"而静默通过。
    """
    try:
        resp = httpx.post(
            f"{API}/__test__/stub-tickets",
            json={"kind": kind, "ticket_id": ticket_id},
            timeout=10.0,
            trust_env=False,
        )
    except Exception as exc:  # noqa: BLE001
        check(f"登记桩名单 {kind}={ticket_id}", False, f"请求异常：{type(exc).__name__}: {exc}")
        return
    check(
        f"登记桩名单 {kind}={ticket_id}",
        resp.status_code == 200,
        f"HTTP {resp.status_code} {resp.text[:120]}",
    )


# ======================================================================
# main
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="并发控制验收（同工单互斥 / 不同工单并发 / 断点恢复）")
    parser.add_argument("--only", default="all", help="all | 1 | 2 | 3 | 4 | 5 | resume | retry | zombie")
    parser.add_argument("--keep", action="store_true", help="跑完不关闭测试实例")
    args = parser.parse_args()

    only = str(args.only).lower()
    want = lambda key: only in ("all", key)  # noqa: E731

    print(f"并发控制验收：测试实例 {BASE}（确定性 LLM 桩，delay={STUB_DELAY}s/次模型调用）")
    ok, proc = ensure_test_instance()
    if not ok:
        return 2

    try:
        api = Api()
        api.register_and_login("acc_conc")
        cs = Api()
        cs.login("service01", "123456")

        if want("1"):
            test_same_ticket(api)
        if want("2"):
            test_different_tickets(api)
        if want("3"):
            test_agent_failure_does_not_stick(api)
        if want("4"):
            test_waiting_human(api, cs)
        if want("5"):
            test_race_same_ticket(api)
        if want("resume"):
            test_resume_from_checkpoint(api)
            # 再来一遍，换一个更靠后的节点：验证"续跑能跳过多个已完成的节点"
            test_resume_from_checkpoint(api, node="generate_result")
        if want("retry"):
            test_retry_policy(api)
        if want("zombie"):
            test_zombie_execution(api)
    finally:
        if proc is not None and not args.keep:
            proc.terminate()

    section("汇总")
    print(f"  通过 {len(_passed)} 项 / 失败 {len(_failed)} 项 / 跳过 {len(_skipped)} 项")
    for name in _failed:
        print(f"    ✗ {name}")
    for name in _skipped:
        print(f"    ○ {name}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
