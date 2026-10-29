"""端到端链路验收：**下单 → 支付 → 售后 → 工单 → Agent → 退款 → 结单**。

    cd backend
    uv run python scripts/e2e_chain.py                # 自动退款链路（不需要 LLM）
    uv run python scripts/e2e_chain.py --with-llm     # 用真实 LLM 跑 analyze/result 两个节点

和另外三个脚本的分工
--------------------
    acceptance.py          业务链路 + 异常安全（真实 LLM 与故障实例，最重）
    concurrency_check.py   Agent 执行的并发控制（互斥 / 断点恢复）
    concurrency_safety.py  资金与库存的并发安全（支付 / 退款 / 库存 / 状态机）
    e2e_chain.py（本脚本） **一条链路走到底**：从下单到结单，并逐环节断言状态与账目

为什么还要它：前面几个脚本各自验证"某一块对不对"，但没有人验证"整条链串起来对不对"——
比如退款之后售后单是不是也结单了、库存有没有回补、工单有没有推进到结束态、
幂等键有没有留下记录。这些跨模块的一致性只有端到端跑一遍才看得出来。

默认用确定性 LLM 桩（不需要 LLM_API_KEY、不花钱、结果稳定）；
--with-llm 则用 .env 里配的真实模型 —— 那才是"Agent 真的会自己判断"的证据。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 默认跑在带 LLM 桩的测试实例上（:8011，避免和并发测试的 :8010 打架）
STUB_PORT = 8011
STUB_BASE = f"http://127.0.0.1:{STUB_PORT}"
MAIN_BASE = "http://127.0.0.1:8000"

CS_LOGIN = {"username": "service01", "password": "123456"}
ADMIN_LOGIN = {"username": "admin", "password": "admin123"}

_passed: list[str] = []
_failed: list[str] = []


# ======================================================================
# 输出与断言
# ======================================================================
def section(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    (_passed if ok else _failed).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))
    return ok


def note(text: str) -> None:
    print(f"      · {text}")


# ======================================================================
# API / DB
# ======================================================================
class Api:
    def __init__(self, base: str) -> None:
        self.client = httpx.Client(base_url=f"{base}/api", timeout=180.0, trust_env=False)
        self.token: str | None = None

    def login(self, username: str, password: str) -> dict:
        resp = self.client.post("/auth/login", json={"username": username, "password": password})
        resp.raise_for_status()
        payload = resp.json()
        self.token = payload["access_token"]
        return payload["user"]

    def register_and_login(self, prefix: str) -> str:
        username = f"{prefix}_{uuid.uuid4().hex[:8]}"
        self.client.post(
            "/auth/register", json={"username": username, "password": "123456", "nickname": username}
        ).raise_for_status()
        self.login(username, "123456")
        return username

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
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


def one(sql: str, params: tuple = ()) -> dict | None:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


# ======================================================================
# 测试实例
# ======================================================================
def _reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3.0, trust_env=False).status_code < 500
    except Exception:  # noqa: BLE001
        return False


def ensure_instance(base: str, *, stub: bool) -> tuple[bool, object | None]:
    health = f"{base}/health"
    if _reachable(health):
        print(f"  （复用已在运行的实例 {base}）")
        return True, None

    port = base.rsplit(":", 1)[-1]
    env = {**os.environ}
    if stub:
        env.update(
            {
                "AGENT_STUB_LLM": "1",
                "AGENT_STUB_DELAY_SECONDS": "0.05",
                # 端到端链路要验证"Agent 真的把钱退了"，所以让桩下发 apply_refund 动作
                "AGENT_STUB_BUSINESS_ACTION": "apply_refund",
            }
        )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", port],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        if _reachable(health):
            print(f"  （已拉起实例 {base}{'（确定性 LLM 桩）' if stub else '（真实 LLM）'}）")
            return True, proc
        time.sleep(0.5)
    proc.terminate()
    return False, None


# ======================================================================
# 链路
# ======================================================================
def run_chain(base: str) -> None:
    section("链路：下单 → 支付 → 售后 → 工单 → Agent → 自动退款 → 结单")
    user = Api(base)
    cs = Api(base)
    admin = Api(base)
    admin.login(**ADMIN_LOGIN)
    cs_user = cs.login(**CS_LOGIN)

    username = user.register_and_login("acc_e2e")
    check("① 注册并登录", bool(user.token), username)

    # ---- ② 商品与库存 ----
    product = admin.ok(
        "POST",
        "/admin/products",
        json={
            "name": f"端到端链路商品_{uuid.uuid4().hex[:6]}",
            "price": 120.0,
            "stock": 10,
            "description": "e2e_chain 用",
        },
    )
    check("② 管理员建商品（库存 10）", product["stock"] == 10, f"id={product['product_id']}")

    # ---- ③ 加购 + 下单 ----
    item = user.ok("POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 2})
    order = user.ok("POST", "/orders", json={"item_ids": [item["item_id"]]})
    check(
        "③ 下单成功（金额 = 单价 × 2）",
        float(order["total_amount"]) == 240.0,
        f"{order['order_no']} 金额={order['total_amount']}",
    )
    stock_after_order = one("SELECT stock FROM products WHERE id=%s", (product["product_id"],))
    check("③ 下单扣了库存", int(stock_after_order["stock"]) == 8, f"库存={stock_after_order['stock']}")

    # ---- ④ 支付 ----
    paid = user.ok("POST", f"/orders/{order['order_id']}/pay")
    check("④ 支付成功", paid["payment_status"] == "PAID", paid["payment_status"])

    # ---- ⑤ 申请售后 ----
    sale = user.ok(
        "POST",
        "/after-sales",
        json={"order_id": order["order_id"], "type": "REFUND", "reason": "端到端链路：不想要了"},
    )
    check("⑤ 创建售后单（APPLYING）", sale["status"] == "APPLYING", f"{sale['after_sale_no']}")

    # ---- ⑥ 提工单（带订单 + 售后上下文）----
    ticket = user.ok(
        "POST",
        "/tickets",
        json={
            "content": f"订单 {order['order_no']} 我申请退款，请尽快处理",
            "order_id": order["order_id"],
            "after_sale_id": sale["after_sale_id"],
        },
    )
    check("⑥ 创建工单", ticket["ticket_id"] > 0, f"ticket={ticket['ticket_id']} {ticket['status']}")
    check(
        "⑥ 工单首条消息记录了用户原话（TicketMessage 统一消息流）",
        bool(ticket["messages"]) and ticket["messages"][0]["sender_type"] == "USER",
        f"{len(ticket['messages'])} 条消息",
    )

    # ---- ⑦ Agent 处理 ----
    processed = user.ok("POST", f"/tickets/{ticket['ticket_id']}/process")
    note(f"AgentRun status={processed['status']} run={processed['agent_run_id']}")
    check("⑦ Agent 处理完成（COMPLETED）", processed["status"] == "COMPLETED", str(processed["status"]))

    run = one(
        "SELECT status, current_node, used_rag, used_tool, retry_count, error "
        "FROM agent_runs WHERE id=%s",
        (processed["agent_run_id"],),
    )
    check("⑦ AgentRun 落库为 COMPLETED", run["status"] == "COMPLETED", str(run["status"]))
    check(
        "⑦ 执行结束后 current_node 已清空（不会让人误以为还在跑）",
        run["current_node"] is None,
        str(run["current_node"]),
    )
    check("⑦ Agent 用了 RAG（used_rag）", bool(run["used_rag"]), str(run["used_rag"]))

    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_type, message FROM agent_run_events WHERE agent_run_id=%s ORDER BY id",
                (processed["agent_run_id"],),
            )
            event_rows = cur.fetchall()
    finally:
        conn.close()
    event_types = [row["event_type"] for row in event_rows]
    check(
        "⑦ 过程事件齐全（分析 → 检索 → 生成 → 执行 → 完成）",
        {"ANALYZING", "RETRIEVING_KNOWLEDGE", "RESULT_GENERATED", "EXECUTING_REFUND", "COMPLETED"}
        <= set(event_types),
        " / ".join(event_types),
    )

    # ---- ⑧ 退款真的落地 ----
    final_order = user.ok("GET", f"/orders/{order['order_id']}")
    check("⑧ 订单已退款", final_order["payment_status"] == "REFUNDED", final_order["payment_status"])

    refunds = one(
        "SELECT COUNT(1) AS n FROM refund_records WHERE order_id=%s AND status='SUCCESS'",
        (order["order_id"],),
    )
    check("⑧ 恰好一笔成功退款流水", int(refunds["n"]) == 1, f"{refunds['n']} 笔")

    sale_detail = user.ok("GET", f"/after-sales/{sale['after_sale_id']}")
    check("⑧ 售后单结单", sale_detail["status"] == "COMPLETED", sale_detail["status"])

    stock_final = one("SELECT stock FROM products WHERE id=%s", (product["product_id"],))
    check("⑧ 未发货退款回补库存", int(stock_final["stock"]) == 10, f"库存={stock_final['stock']}")

    # ---- ⑨ 副作用幂等键留痕 ----
    claims = one(
        "SELECT COUNT(1) AS n FROM side_effect_claims WHERE kind='refund' AND target=%s",
        (f"order:{order['order_id']}",),
    )
    check(
        "⑨ 退款留下了幂等键记录（同一次执行不可能退第二次）",
        int(claims["n"]) == 1,
        f"{claims['n']} 条 side_effect_claims",
    )

    # ---- ⑩ 工单与消息收尾 ----
    ticket_detail = user.ok("GET", f"/tickets/{ticket['ticket_id']}")
    check(
        "⑩ 工单推进到结束态（RESOLVED/CLOSED）",
        ticket_detail["status"] in ("RESOLVED", "CLOSED"),
        ticket_detail["status"],
    )
    check(
        "⑩ 工单执行权已释放（agent_status=IDLE）",
        ticket_detail["agent_status"] == "IDLE",
        ticket_detail["agent_status"],
    )
    check(
        "⑩ Agent 的回复进了统一消息流（sender_type=AGENT）",
        any(m["sender_type"] == "AGENT" for m in ticket_detail["messages"]),
        f"{len(ticket_detail['messages'])} 条消息",
    )

    # ---- ⑪ 幂等：再点一次处理不会重复退款 ----
    again = user.call("POST", f"/tickets/{ticket['ticket_id']}/process")
    refunds_again = one(
        "SELECT COUNT(1) AS n FROM refund_records WHERE order_id=%s", (order["order_id"],)
    )
    check(
        "⑪ 链路结束后再次处理：不会产生第二笔退款",
        int(refunds_again["n"]) == 1,
        f"HTTP {again.status_code} 退款流水仍为 {refunds_again['n']} 笔",
    )

    # ---- ⑫ 客服视角能看到完整过程 ----
    staff_view = cs.ok("GET", f"/customer-service/tickets/{ticket['ticket_id']}")
    check(
        "⑫ 客服工作台能看到 Agent 过程与执行记录",
        bool(staff_view["agent_events"]) and bool(staff_view["agent_runs"]),
        f"事件={len(staff_view['agent_events'])} 执行={len(staff_view['agent_runs'])}",
    )
    check(
        "⑫ 客服能看到工单关联的订单上下文",
        staff_view.get("order_id") == order["order_id"],
        str(staff_view.get("order_id")),
    )


# ======================================================================
# main
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="端到端链路验收（下单→支付→售后→工单→Agent→退款）")
    parser.add_argument("--with-llm", action="store_true", help="用真实 LLM（需配置 LLM_API_KEY）")
    parser.add_argument("--keep", action="store_true", help="跑完不关闭测试实例")
    args = parser.parse_args()

    base = MAIN_BASE if args.with_llm else STUB_BASE
    print(f"端到端链路验收：{base}{'（真实 LLM）' if args.with_llm else '（确定性 LLM 桩）'}")

    ok, proc = ensure_instance(base, stub=not args.with_llm)
    if not ok:
        print(f"✗ 实例不可用：{base}")
        return 2

    try:
        run_chain(base)
    finally:
        if proc is not None and not args.keep:
            proc.terminate()

    section("汇总")
    print(f"  通过 {len(_passed)} 项 / 失败 {len(_failed)} 项")
    for name in _failed:
        print(f"    ✗ {name}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
