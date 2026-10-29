"""业务并发安全验收：**重复支付 / 重复退款 / 库存并发扣减 / 非法状态修改**。

    cd backend
    uv run python scripts/concurrency_safety.py          # 全部
    uv run python scripts/concurrency_safety.py --only stock

它和另外两个脚本的分工
----------------------
    scripts/acceptance.py       业务链路对不对（真实 LLM、场景 A~D）
    scripts/concurrency_check.py Agent 执行的并发控制（同工单互斥 / 断点恢复）
    scripts/concurrency_safety.py（本脚本）**资金与库存的并发安全**：
        重复支付、重复退款、超卖、非法状态转换 —— 主要靠
        事务 + 行锁 + DB 条件更新 + CHECK 约束

为什么单独一个脚本：这几条是"钱和货"的不变量，最该被反复验证，
而且它们全部可以用 HTTP 黑盒 + 直连数据库断言，不需要 LLM、跑得很快。

前置：后端已在 :8000 运行（`uv run uvicorn app.main:app`），需要真实 MySQL。
本脚本只写测试数据（随机账号 / 订单 / 商品），不删已有数据。
"""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000/api"
HEALTH = "http://127.0.0.1:8000/health"

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
# HTTP / 数据库
# ======================================================================
class Api:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=API, timeout=60.0, trust_env=False)
        self.token: str | None = None

    def login(self, username: str, password: str) -> dict:
        resp = self.client.post("/auth/login", json={"username": username, "password": password})
        resp.raise_for_status()
        payload = resp.json()
        self.token = payload["access_token"]
        return payload["user"]

    def register_and_login(self, prefix: str) -> None:
        username = f"{prefix}_{uuid.uuid4().hex[:8]}"
        self.client.post(
            "/auth/register", json={"username": username, "password": "123456", "nickname": username}
        ).raise_for_status()
        self.login(username, "123456")

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        return self.client.request(method, path, headers=self.headers(), **kw)

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
            return cur.fetchall()
    finally:
        conn.close()


def parallel_post(paths_and_headers: list[tuple[str, dict]], count: int) -> list[int]:
    """对同一路径同时发 count 个请求，返回状态码列表（用 Barrier 对齐起跑线）。"""
    barrier = threading.Barrier(count)
    statuses: list[int] = [0] * count

    def one(index: int) -> None:
        path, headers = paths_and_headers[0]
        with httpx.Client(base_url=API, timeout=90.0, trust_env=False) as raw:
            barrier.wait()
            statuses[index] = raw.post(path, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(one, range(count)))
    return statuses


# ======================================================================
# 准备：建商品 / 下单 / 支付
# ======================================================================
def admin_api() -> Api:
    api = Api()
    api.login(**ADMIN_LOGIN)
    return api


def cs_api() -> Api:
    api = Api()
    api.login(**CS_LOGIN)
    return api


def create_product(admin: Api, *, price: float, stock: int) -> dict:
    name = f"并发测试商品_{uuid.uuid4().hex[:6]}"
    return admin.ok(
        "POST",
        "/admin/products",
        json={"name": name, "price": price, "stock": stock, "description": "并发安全验收用"},
    )


def buy(api: Api, product_id: int, quantity: int = 1) -> dict:
    """加购 → 下单（不支付），返回订单详情。"""
    item = api.ok("POST", "/cart/items", json={"product_id": product_id, "quantity": quantity})
    return api.ok("POST", "/orders", json={"item_ids": [item["item_id"]]})


# ======================================================================
# 测试 1：重复支付（并发双击）
# ======================================================================
def test_duplicate_payment() -> None:
    section("测试 1：重复支付 —— 并发双击只能成功一次，且只产生一笔支付流水")
    user = Api()
    user.register_and_login("acc_pay")
    admin = admin_api()

    product = create_product(admin, price=88.0, stock=5)
    order = buy(user, product["product_id"])
    note(f"订单 {order['order_no']}，金额 {order['total_amount']}")

    statuses = parallel_post([(f"/orders/{order['order_id']}/pay", user.headers())], 4)
    check(
        "4 个并发支付请求：恰好 1 个成功，其余 409",
        sorted(statuses) == [200, 409, 409, 409],
        f"状态码={sorted(statuses)}",
    )

    payments = query_all(
        "SELECT id, status, amount FROM payments WHERE order_id=%s", (order["order_id"],)
    )
    check("数据库里只有 1 笔支付流水", len(payments) == 1, f"payments={len(payments)}")
    check(
        "支付流水是 SUCCESS",
        len(payments) == 1 and payments[0]["status"] == "SUCCESS",
        str(payments[0]["status"] if payments else "-"),
    )

    row = query_one("SELECT payment_status, fulfillment_status FROM orders WHERE id=%s", (order["order_id"],))
    check(
        "订单进入 PAID/PENDING（资金状态只变了一次）",
        row["payment_status"] == "PAID",
        f"{row['payment_status']}/{row['fulfillment_status']}",
    )


# ======================================================================
# 测试 2：重复退款（并发申请）
# ======================================================================
def test_duplicate_refund() -> None:
    section("测试 2：重复退款 —— 并发申请只能成功一次，且只产生一笔退款流水")
    user = Api()
    user.register_and_login("acc_refund")
    admin = admin_api()

    product = create_product(admin, price=66.0, stock=5)
    order = buy(user, product["product_id"])
    user.ok("POST", f"/orders/{order['order_id']}/pay")

    sale = user.ok(
        "POST",
        "/after-sales",
        json={"order_id": order["order_id"], "type": "REFUND", "reason": "并发退款验收"},
    )
    note(f"订单 {order['order_no']} / 售后单 {sale['after_sale_no']}")

    statuses = parallel_post(
        [(f"/after-sales/{sale['after_sale_id']}/refund", user.headers())], 4
    )
    check(
        "4 个并发退款请求：恰好 1 个成功，其余被幂等挡住（409）",
        statuses.count(200) == 1 and all(code in (200, 409) for code in statuses),
        f"状态码={sorted(statuses)}",
    )

    refunds = query_all(
        "SELECT id, status, amount FROM refund_records WHERE order_id=%s", (order["order_id"],)
    )
    check("数据库里只有 1 笔退款流水", len(refunds) == 1, f"refunds={len(refunds)}")
    check(
        "退款流水是 SUCCESS",
        len(refunds) == 1 and refunds[0]["status"] == "SUCCESS",
        str(refunds[0]["status"] if refunds else "-"),
    )

    row = query_one("SELECT payment_status FROM orders WHERE id=%s", (order["order_id"],))
    check(
        "订单最终是 REFUNDED（没有出现「退了两次」）",
        row["payment_status"] == "REFUNDED",
        row["payment_status"],
    )

    # 退款后库存应当回补了一次（未发货退款）
    stock = query_one("SELECT stock FROM products WHERE id=%s", (product["product_id"],))
    check(
        "未发货退款回补库存一次（不是两次）",
        int(stock["stock"]) == 5,
        f"库存={stock['stock']}（下单扣 1 → 4，退款回补 1 → 5）",
    )


# ======================================================================
# 测试 3：库存并发扣减（防超卖）
# ======================================================================
def test_stock_race() -> None:
    section("测试 3：库存并发扣减 —— 10 个人抢 3 件库存，只能卖出 3 件（不超卖）")
    admin = admin_api()
    product = create_product(admin, price=10.0, stock=3)
    note(f"商品 {product['name']}（id={product['product_id']}）初始库存 3")

    # 每个用户只买 1 件，10 个用户同时下单
    users: list[Api] = []
    for index in range(10):
        api = Api()
        api.register_and_login(f"acc_stock{index}_")
        api.ok("POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 1})
        users.append(api)

    barrier = threading.Barrier(len(users))
    results: list[int] = [0] * len(users)

    def place_order(index: int) -> None:
        api = users[index]
        cart = api.ok("GET", "/cart")
        item_ids = [item["item_id"] for item in cart["items"] if item["product_id"] == product["product_id"]]
        barrier.wait()
        resp = api.call("POST", "/orders", json={"item_ids": item_ids})
        results[index] = resp.status_code

    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        list(pool.map(place_order, range(len(users))))

    succeeded = sum(1 for code in results if 200 <= code < 300)
    rejected = sum(1 for code in results if code >= 400)
    check(
        "恰好 3 单成功、其余被库存不足拒绝",
        succeeded == 3 and rejected == 7,
        f"成功={succeeded} 失败={rejected} 状态码={sorted(results)}",
    )

    stock = query_one("SELECT stock FROM products WHERE id=%s", (product["product_id"],))
    check("库存精确归零（没有变成负数）", int(stock["stock"]) == 0, f"库存={stock['stock']}")

    sold = query_one(
        "SELECT COUNT(1) AS n FROM order_items WHERE product_id=%s", (product["product_id"],)
    )
    check("卖出的明细条数 = 3（与库存扣减一致）", int(sold["n"]) == 3, f"明细={sold['n']} 条")

    # 再补一刀：数据库自己也拦得住负数库存
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE products SET stock = -1 WHERE id=%s", (product["product_id"],))
        conn.commit()
        check("数据库拒绝把库存改成负数（CHECK 约束生效）", False, "居然改成功了")
    except Exception as exc:  # noqa: BLE001
        check("数据库拒绝把库存改成负数（CHECK 约束生效）", "check" in str(exc).lower() or "3819" in str(exc), str(exc)[:90])
    finally:
        conn.close()


# ======================================================================
# 测试 4：非法状态修改（跳过状态机的请求必须被拒）
# ======================================================================
def test_illegal_state_changes() -> None:
    section("测试 4：非法状态修改 —— 绕过状态机的请求一律被拒（并发也改不出脏状态）")
    user = Api()
    user.register_and_login("acc_state")
    cs = cs_api()
    admin = admin_api()

    product = create_product(admin, price=20.0, stock=5)
    order = buy(user, product["product_id"])
    order_id = order["order_id"]

    # ① 未支付 → 不能发货（并发 3 次也一样）
    statuses = parallel_post([(f"/customer-service/orders/{order_id}/ship", cs.headers())], 3)
    check(
        "未支付订单并发发货：全部被拒",
        all(code >= 400 for code in statuses),
        f"状态码={sorted(statuses)}",
    )

    # ② 未支付 → 不能确认收货
    resp = user.call("POST", f"/orders/{order_id}/confirm")
    check("未发货订单不可确认收货", resp.status_code in (400, 409), f"{resp.status_code}")

    # ③ 未支付 → 不能退款
    resp = user.call(
        "POST",
        "/after-sales",
        json={"order_id": order_id, "type": "REFUND", "reason": "未支付退款"},
    )
    if resp.status_code < 300:
        sale_id = resp.json()["after_sale_id"]
        resp = user.call("POST", f"/after-sales/{sale_id}/refund")
    check("未支付订单不可退款", resp.status_code in (400, 409), f"{resp.status_code}")

    # ④ 已支付 → 不能取消（并发取消也不行）
    user.ok("POST", f"/orders/{order_id}/pay")
    statuses = parallel_post([(f"/orders/{order_id}/cancel", user.headers())], 3)
    check(
        "已支付订单并发取消：全部被拒（不会把已支付订单取消掉）",
        all(code >= 400 for code in statuses),
        f"状态码={sorted(statuses)}",
    )
    row = query_one(
        "SELECT payment_status, fulfillment_status FROM orders WHERE id=%s", (order_id,)
    )
    check(
        "订单状态没有被并发取消改脏",
        row["payment_status"] == "PAID" and row["fulfillment_status"] != "CANCELLED",
        f"{row['payment_status']}/{row['fulfillment_status']}",
    )

    # ⑤ 已支付 → 不能重复支付
    resp = user.call("POST", f"/orders/{order_id}/pay")
    check("已支付订单不可重复支付", resp.status_code == 409, f"{resp.status_code}")

    # ⑥ 越权：别人不能操作我的订单
    other = Api()
    other.register_and_login("acc_state_other")
    for method, path in (("POST", f"/orders/{order_id}/pay"), ("POST", f"/orders/{order_id}/cancel"),
                         ("POST", f"/orders/{order_id}/confirm")):
        resp = other.call(method, path)
        check(f"越权 {path.split('/')[-1]} → 被拒", resp.status_code in (403, 404), f"{resp.status_code}")


# ======================================================================
# 测试 5：支付与发货并发交错（状态机前置条件会被并发绕过吗）
# ======================================================================
def test_pay_and_ship_race() -> None:
    """同一订单"支付"和"发货"同时打进来。

    靶子为什么选这一对：业务规则要求"申请售后/退款必须先支付"，所以并发退款天然被挡；
    而"发货"只要求 order.payment_status == PAID —— 正好可以和"支付"并发抢同一条订单。
    不变式：**绝不能出现"未支付却已发货"**。要么发货被前置校验挡掉（409），
    要么支付先提交、发货随后成功 —— 两种都自洽。
    """
    section("测试 5：支付与发货并发交错 —— 绝不出现「未支付却已发货」")
    user = Api()
    user.register_and_login("acc_mix")
    cs = cs_api()
    admin = admin_api()

    product = create_product(admin, price=30.0, stock=5)
    order = buy(user, product["product_id"])
    order_id = order["order_id"]

    barrier = threading.Barrier(2)
    codes: dict[str, int] = {}

    def do_pay() -> None:
        with httpx.Client(base_url=API, timeout=60.0, trust_env=False) as raw:
            barrier.wait()
            codes["pay"] = raw.post(f"/orders/{order_id}/pay", headers=user.headers()).status_code

    def do_ship() -> None:
        with httpx.Client(base_url=API, timeout=60.0, trust_env=False) as raw:
            barrier.wait()
            codes["ship"] = raw.post(
                f"/customer-service/orders/{order_id}/ship", headers=cs.headers()
            ).status_code

    threads = [threading.Thread(target=do_pay), threading.Thread(target=do_ship)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    row = query_one(
        "SELECT payment_status, fulfillment_status FROM orders WHERE id=%s", (order_id,)
    )
    note(
        f"支付={codes.get('pay')} 发货={codes.get('ship')} "
        f"订单={row['payment_status']}/{row['fulfillment_status']}"
    )

    shipped_statuses = ("SHIPPED", "DELIVERED", "COMPLETED")
    check(
        "状态自洽：已发货的订单一定已支付",
        not (row["fulfillment_status"] in shipped_statuses and row["payment_status"] != "PAID"),
        f"{row['payment_status']}/{row['fulfillment_status']}",
    )
    check(
        "资金流水与订单状态一致（PAID 才有成功的支付流水）",
        (row["payment_status"] == "PAID")
        == bool(query_all("SELECT id FROM payments WHERE order_id=%s AND status='SUCCESS'", (order_id,))),
        f"payment_status={row['payment_status']}",
    )
    check(
        "并发没有把订单改成非法组合",
        row["payment_status"] in ("UNPAID", "PAID")
        and row["fulfillment_status"] in ("PENDING", "SHIPPED"),
        f"{row['payment_status']}/{row['fulfillment_status']}",
    )


# ======================================================================
# main
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="业务并发安全验收（支付/退款/库存/状态机）")
    parser.add_argument("--only", default="all", help="all | pay | refund | stock | state | mix")
    args = parser.parse_args()
    only = str(args.only).lower()
    want = lambda key: only in ("all", key)  # noqa: E731

    try:
        httpx.get(HEALTH, timeout=3.0, trust_env=False)
    except Exception:  # noqa: BLE001
        print(f"✗ 后端未启动：{HEALTH} 不可达。先运行：cd backend && uv run uvicorn app.main:app")
        return 2

    print(f"业务并发安全验收：{API}")

    if want("pay"):
        test_duplicate_payment()
    if want("refund"):
        test_duplicate_refund()
    if want("stock"):
        test_stock_race()
    if want("state"):
        test_illegal_state_changes()
    if want("mix"):
        test_pay_and_ship_race()

    section("汇总")
    print(f"  通过 {len(_passed)} 项 / 失败 {len(_failed)} 项")
    for name in _failed:
        print(f"    ✗ {name}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
