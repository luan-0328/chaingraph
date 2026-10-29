"""并发与幂等：钱和货的不变量（v2.md §59 / §60）。

本文件覆盖需求里最不能出错的那几条：

    · 并发支付只能成功一次，且只产生一笔资金流水
    · 并发退款只能成功一次，且只产生一笔退款流水、库存只回补一次
    · 重复请求（串行重放）被业务码挡住，不产生第二次副作用
    · 数据库层的唯一约束是**真的活的**（不是只靠 Service 记得查一下）
    · 库存并发扣减不超卖、不出现负库存

为什么同时断言"HTTP 码"和"数据库行数"
    只看 HTTP 码会被"两个请求都返回 200 但其实只退了一次"骗过；
    只看行数又无法区分"被正确拒绝"和"悄悄什么都没做"。
    所以两边都断言：**恰好一个 200，其余 409，且库里恰好一行**。
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    Api,
    Server,
    buy,
    concurrent,
    create_after_sale,
    create_product,
    execute,
    order_row,
    payment_count,
    product_stock,
    query_one,
    refund_count,
)

pytestmark = pytest.mark.integration


# ======================================================================
# 支付
# ======================================================================
class TestConcurrentPayment:
    """并发支付：4 个请求同时打进来，只能收到一次钱。"""

    def test_concurrent_pay_succeeds_exactly_once(self, user: Api, admin: Api) -> None:
        product = create_product(admin, price=88.0, stock=5)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        responses = concurrent("POST", user.base_url, f"/orders/{order_id}/pay", user.token, 4)
        codes = sorted(resp.status_code for resp in responses)

        assert codes.count(200) == 1, f"应当恰好一个成功，实际 {codes}"
        assert all(code in (200, 409) for code in codes), f"出现非预期状态码：{codes}"

        # ---- 数据库侧：钱只动了一次 ----
        assert payment_count(order_id) == 1, "并发支付产生了多笔支付流水"
        assert payment_count(order_id, status="SUCCESS") == 1
        assert order_row(order_id)["payment_status"] == "PAID"

    def test_concurrent_pay_returns_business_code(self, user: Api, admin: Api) -> None:
        """被挡住的那几个请求要给出**可读的业务码**，而不是模糊的 500。"""
        product = create_product(admin, price=10.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        responses = concurrent("POST", user.base_url, f"/orders/{order_id}/pay", user.token, 3)
        rejected = [resp for resp in responses if resp.status_code == 409]
        assert rejected, "至少要有请求被幂等挡住"

        codes = {resp.json().get("code") for resp in rejected}
        assert codes <= {"ALREADY_PAID", "PAYMENT_IN_PROGRESS", "DUPLICATE_OPERATION"}, codes

    def test_sequential_replay_is_idempotent(self, user: Api, admin: Api) -> None:
        """串行重复请求（用户连点两次"支付"）：第二次必须被拒，不产生第二笔流水。"""
        product = create_product(admin, price=20.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        assert user.status("POST", f"/orders/{order_id}/pay") == 200
        second = user.call("POST", f"/orders/{order_id}/pay")

        assert second.status_code == 409
        assert second.json()["code"] == "ALREADY_PAID"
        assert payment_count(order_id) == 1


# ======================================================================
# 退款
# ======================================================================
class TestConcurrentRefund:
    """并发退款：这是"钱"上最容易出事的地方，也是三层防护的靶场。"""

    def test_concurrent_refund_succeeds_exactly_once(self, user: Api, admin: Api) -> None:
        product = create_product(admin, price=60.0, stock=5)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        sale = create_after_sale(user, order_id)

        responses = concurrent(
            "POST",
            user.base_url,
            f"/after-sales/{sale['after_sale_id']}/refund",
            user.token,
            4,
        )
        codes = sorted(resp.status_code for resp in responses)

        assert codes.count(200) == 1, f"应当恰好一个退款成功，实际 {codes}"

        assert refund_count(order_id) == 1, "并发退款产生了多笔退款流水"
        assert refund_count(order_id, status="SUCCESS") == 1
        assert order_row(order_id)["payment_status"] == "REFUNDED"

    def test_refund_restores_stock_exactly_once(self, user: Api, admin: Api) -> None:
        """未发货退款要回补库存 —— 并发下绝不能补两次（那是白送商品）。"""
        product = create_product(admin, price=30.0, stock=6)
        product_id = product["product_id"]
        order = buy(user, product_id)
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        stock_after_order = product_stock(product_id)
        sale = create_after_sale(user, order_id)

        concurrent(
            "POST",
            user.base_url,
            f"/after-sales/{sale['after_sale_id']}/refund",
            user.token,
            4,
        )

        # 下单扣 1 → 退款回补 1，必须精确回到下单前
        assert product_stock(product_id) == stock_after_order + 1, "库存回补次数不对（补了两次？）"

    def test_second_refund_after_success_is_rejected(self, user: Api, admin: Api) -> None:
        """已经退过的订单再申请：业务码必须是 ALREADY_REFUNDED，且流水仍是一条。"""
        product = create_product(admin, price=45.0, stock=4)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        sale = create_after_sale(user, order_id)

        assert user.status("POST", f"/after-sales/{sale['after_sale_id']}/refund") == 200

        replay = user.call("POST", f"/after-sales/{sale['after_sale_id']}/refund")
        assert replay.status_code == 409
        assert replay.json()["code"] in ("ALREADY_REFUNDED", "DUPLICATE_REFUND")
        assert refund_count(order_id) == 1


# ======================================================================
# 数据库层：唯一约束真的是活的
# ======================================================================
class TestDatabaseLevelGuards:
    """直接往库里插脏数据，证明**数据库**这一层挡得住。

    为什么必须单独测这一层：Service 的条件更新才是主防线，
    但"主防线被绕过时数据库还兜得住吗"只有直连数据库插数据才能回答。
    这几条断言如果失效，说明 uq_*_active_order 索引名存实亡。

    注意断言的是 `pymysql.Error` 而不是 SQLAlchemy 的 `IntegrityError`：
    这些用例走的是 conftest 里的**裸 pymysql** 连接（就是要绕开 ORM 与 Service），
    所以拿到的是驱动原生异常。混用会把"数据库约束真的拦住了"
    和"ORM 层拦住了"搞混 —— 而我们要证的恰恰是前者。
    """

    def test_duplicate_active_payment_rejected_by_db(self, user: Api, admin: Api) -> None:
        import pymysql

        product = create_product(admin, price=15.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")

        with pytest.raises(pymysql.Error) as exc:
            execute(
                "INSERT INTO payments (order_id, payment_no, amount, status, created_at, updated_at) "
                "VALUES (%s, %s, 1.00, 'SUCCESS', UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                (order_id, f"DUP{order_id}{id(self)}"),
            )
        assert "uq_payments_active_order" in str(exc.value), str(exc.value)
        assert payment_count(order_id) == 1

    def test_duplicate_active_refund_rejected_by_db(self, user: Api, admin: Api) -> None:
        import pymysql

        product = create_product(admin, price=25.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        sale = create_after_sale(user, order_id)
        user.ok("POST", f"/after-sales/{sale['after_sale_id']}/refund")

        with pytest.raises(pymysql.Error) as exc:
            execute(
                "INSERT INTO refund_records "
                "(refund_no, after_sale_id, order_id, user_id, amount, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, 1.00, 'PENDING', UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                (
                    f"DUPREF{order_id}{id(self)}",
                    sale["after_sale_id"],
                    order_id,
                    user.ok("GET", "/auth/me")["user_id"],
                ),
            )
        assert "uq_refund_records_active_order" in str(exc.value), str(exc.value)
        assert refund_count(order_id) == 1

    def test_failed_payment_attempts_do_not_occupy_the_order(self, user: Api, admin: Api) -> None:
        """FAILED 的支付尝试不占位 —— 否则"第一次支付失败"会变成"永远不能支付"。"""
        product = create_product(admin, price=12.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        for index in range(3):
            execute(
                "INSERT INTO payments (order_id, payment_no, amount, status, created_at, updated_at) "
                "VALUES (%s, %s, 12.00, 'FAILED', UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                (order_id, f"FAILEDPAY{order_id}_{index}"),
            )

        assert payment_count(order_id, status="FAILED") == 3
        # 关键：占位的是"有效支付"，所以失败三次之后照样能正常支付
        assert user.status("POST", f"/orders/{order_id}/pay") == 200
        assert payment_count(order_id, status="SUCCESS") == 1

    def test_failed_refund_attempts_do_not_occupy_the_order(self, user: Api, admin: Api) -> None:
        """FAILED 的退款尝试同样不占位 —— 失败后要允许重新发起退款。"""
        product = create_product(admin, price=12.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        sale = create_after_sale(user, order_id)
        user_id = user.ok("GET", "/auth/me")["user_id"]

        execute(
            "INSERT INTO refund_records "
            "(refund_no, after_sale_id, order_id, user_id, amount, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, 12.00, 'FAILED', UTC_TIMESTAMP(), UTC_TIMESTAMP())",
            (f"FAILEDREF{order_id}", sale["after_sale_id"], order_id, user_id),
        )

        # 库里有一条 FAILED 退款流水，但不影响真正退款（它没占住这个订单）
        result = user.call("POST", f"/after-sales/{sale['after_sale_id']}/refund")
        assert result.status_code == 200, result.text[:200]
        assert refund_count(order_id, status="SUCCESS") == 1
        assert refund_count(order_id, status="FAILED") == 1


# ======================================================================
# 库存
# ======================================================================
class TestStockRace:
    """真实 HTTP 并发下单：校验订单、售出数量及库存守恒。"""

    @pytest.mark.parametrize("user_count,initial_stock,quantity,round_no", [
        (10, 3, 1, 1),
        (50, 1, 1, 1),
        (100, 10, 1, 1),
        (100, 10, 1, 2),
        (100, 10, 1, 3),
        (50, 100, 1, 1),
        (50, 11, 2, 1),
    ])
    def test_no_oversell_under_concurrency(
        self, admin: Api, record_property, user_count: int,
        initial_stock: int, quantity: int, round_no: int,
    ) -> None:
        import threading
        import json
        from concurrent.futures import ThreadPoolExecutor

        product = create_product(admin, price=50.0, stock=initial_stock)
        product_id = product["product_id"]

        users: list[Api] = []
        orders: list[int] = []
        try:
            for index in range(user_count):
                api = Api(admin.base_url)
                api.register_and_login(f"pytest_race{index}")
                users.append(api)
                api.ok("POST", "/cart/items", json={"product_id": product_id, "quantity": quantity})

            barrier = threading.Barrier(len(users))

            def checkout(index: int):
                api = users[index]
                barrier.wait(timeout=30)
                resp = api.call("POST", "/orders", json={})
                return resp.status_code, resp.json()

            # future.result propagates worker exceptions; no silent thread failures.
            with ThreadPoolExecutor(max_workers=user_count) as pool:
                futures = [pool.submit(checkout, index) for index in range(user_count)]
                responses = [future.result(timeout=210) for future in futures]
            results = [status for status, _ in responses]
            orders = [body["order_id"] for status, body in responses if status == 201]
            expected = min(user_count, initial_stock // quantity)
            assert all(status in (201, 409) for status in results), results
            assert results.count(201) == expected, results
            assert len(set(orders)) == expected
            remaining = product_stock(product_id)
            assert remaining == initial_stock - expected * quantity
            db_result = query_one(
                "SELECT COUNT(DISTINCT o.id) AS orders, COALESCE(SUM(i.quantity),0) AS sold "
                "FROM orders o JOIN order_items i ON i.order_id=o.id WHERE i.product_id=%s",
                (product_id,),
            )
            assert db_result["orders"] == expected
            assert int(db_result["sold"]) == expected * quantity
            assert remaining + int(db_result["sold"]) == initial_stock
            record_property("stock_race", json.dumps({
                "users": user_count, "initial_stock": initial_stock,
                "quantity": quantity, "round": round_no,
                "successful_orders": db_result["orders"], "sold_units": int(db_result["sold"]),
                "final_stock": remaining, "rejected_requests": results.count(409),
            }))
        finally:
            for api in users:
                api.close()

    def test_stock_never_goes_negative(self, admin: Api) -> None:
        """数据库 CHECK 约束兜底：绕过应用层直接改库存也会被拒。

        为什么要单独验：应用层用的是条件更新 `WHERE stock >= ?`（主防线），
        但"主防线被绕过时数据库还兜得住吗"只有裸 SQL 才能回答。
        库里的约束是 `ck_products_stock_non_negative CHECK (stock >= 0)`
        （MySQL 8.0.16+ 才真正强制 CHECK，见 core/schema.py 的版本判断）。

        断言 pymysql.Error 而不是 ORM 异常：这条 UPDATE 走的是**裸连接**，
        要证的正是"不经过应用层也拦得住"。
        """
        import pymysql

        product = create_product(admin, price=10.0, stock=1)
        with pytest.raises(pymysql.Error) as exc:
            execute("UPDATE products SET stock = -1 WHERE id = %s", (product["product_id"],))
        assert "ck_products_stock_non_negative" in str(exc.value), str(exc.value)
        assert product_stock(product["product_id"]) == 1, "库存被改成了负数"

    def test_conditional_stock_update_never_oversells(self, admin: Api) -> None:
        """仓储的条件更新：`UPDATE ... WHERE stock >= n` 抢不到就必须返回 False。"""
        from app.core.database import session_scope
        from app.repositories import product_repository

        product = create_product(admin, price=10.0, stock=2)
        product_id = product["product_id"]

        with session_scope() as db:
            assert product_repository.decrease_stock(db, product_id, 2) is True

        # 库存已归零，再扣一次就是超卖 —— 条件更新返回 False（而不是扣成负数）
        with session_scope() as db:
            assert product_repository.decrease_stock(db, product_id, 1) is False
        assert product_stock(product_id) == 0


# ======================================================================
# 条件更新本身
# ======================================================================
class TestConditionalUpdatePrimitives:
    """直接测 repository 的条件更新原语 —— 它们是并发安全的最内层。"""

    def test_claim_paid_only_wins_once(self, user: Api, admin: Api) -> None:
        from app.core.database import session_scope
        from app.repositories import order_repository

        product = create_product(admin, price=10.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        from app.core.time import utcnow

        with session_scope() as db:
            assert order_repository.claim_paid(db, order_id, utcnow()) is True

        # 第二次必然拿不到那一行（状态已经不是 UNPAID）
        with session_scope() as db:
            assert order_repository.claim_paid(db, order_id, utcnow()) is False

    def test_claim_refunded_requires_paid(self, user: Api, admin: Api) -> None:
        """未支付的订单不能被"退成已退款" —— 条件更新自带状态前置条件。"""
        from app.core.database import session_scope
        from app.core.time import utcnow
        from app.repositories import order_repository

        product = create_product(admin, price=10.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        with session_scope() as db:
            assert order_repository.claim_refunded(db, order_id, refunded_at=utcnow()) is False

        assert order_row(order_id)["payment_status"] == "UNPAID"

    def test_claim_success_requires_pending(self, user: Api, admin: Api) -> None:
        from app.core.database import session_scope
        from app.core.time import utcnow
        from app.repositories import payment_repository

        product = create_product(admin, price=10.0, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")

        payment = query_one(
            "SELECT id, status FROM payments WHERE order_id=%s ORDER BY id DESC LIMIT 1",
            (order_id,),
        )
        assert payment is not None and payment["status"] == "SUCCESS"

        # 已经是 SUCCESS 了，再 claim 一次必须失败（不能重复"确认收款"）
        with session_scope() as db:
            assert payment_repository.claim_success(db, payment["id"], utcnow()) is None
