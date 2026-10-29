"""权限与分层：Tool 只做参数转换，真正的校验全在 Service（v2.md §37 / §58 / §70）。

两条铁律：
    1. **前端隐藏按钮不算权限** —— 每个接口都要在后端重新校验角色与数据归属；
    2. **Agent 没有后门** —— Tool 不负责判断"能不能"，只负责把参数交给 Service，
       Service 再做一遍用户权限、订单归属、状态、金额的完整校验。

所以本文件既打 HTTP 接口（验 Router 层），也直接调 Tool（验"绕过 HTTP 也挡得住"）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import (
    Api,
    Server,
    buy,
    create_after_sale,
    create_product,
    create_ticket,
    order_row,
    refund_count,
)

pytestmark = pytest.mark.integration


# ======================================================================
# 认证
# ======================================================================
class TestAuthentication:
    def test_anonymous_requests_are_401(self, business_server: Server) -> None:
        import httpx

        with httpx.Client(base_url=business_server.api, timeout=30.0, trust_env=False) as raw:
            for method, path in [
                ("GET", "/orders"),
                ("GET", "/tickets"),
                ("GET", "/after-sales"),
                ("GET", "/cart"),
                ("GET", "/auth/me"),
            ]:
                assert raw.request(method, path).status_code == 401, f"{method} {path} 未拦未登录"

    def test_garbage_token_is_401(self, business_server: Server) -> None:
        import httpx

        with httpx.Client(base_url=business_server.api, timeout=30.0, trust_env=False) as raw:
            resp = raw.get("/orders", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401


# ======================================================================
# 角色
# ======================================================================
class TestRoleSeparation:
    def test_user_cannot_reach_admin_endpoints(self, user: Api) -> None:
        for method, path in [
            ("GET", "/admin/products"),
            ("GET", "/admin/users"),
            ("GET", "/admin/orders"),
            ("GET", "/admin/knowledge"),
            ("POST", "/admin/products"),
            ("POST", "/admin/knowledge"),
        ]:
            assert user.status(method, path) == 403, f"{method} {path} 普通用户竟然能访问"

    def test_user_cannot_reach_customer_service_endpoints(self, user: Api) -> None:
        for method, path in [
            ("GET", "/customer-service/dashboard"),
            ("GET", "/customer-service/tickets"),
            ("GET", "/customer-service/users"),
            ("GET", "/customer-service/refunds"),
        ]:
            assert user.status(method, path) == 403, f"{method} {path} 普通用户竟然能访问"

    def test_user_cannot_approve_or_takeover(self, user: Api, admin: Api) -> None:
        """HITL 的三个出口都必须是客服专属 —— 否则用户能自己批准自己的退款。"""
        product = create_product(admin, price=30.0, stock=3)
        order = buy(user, product["product_id"])
        ticket_id = create_ticket(user, content="我要退款", order_id=order["order_id"])

        assert user.status("POST", f"/tickets/{ticket_id}/approve") == 403
        assert user.status("POST", f"/tickets/{ticket_id}/reject") == 403
        assert user.status("POST", f"/tickets/{ticket_id}/takeover") == 403

    def test_staff_cannot_reach_admin_endpoints(self, staff: Api) -> None:
        assert staff.status("GET", "/admin/users") == 403
        assert staff.status("GET", "/admin/knowledge") == 403


# ======================================================================
# 数据归属（跨用户隔离）
# ======================================================================
class TestOwnership:
    def test_cannot_read_another_users_order(self, user: Api, other_user: Api, admin: Api) -> None:
        product = create_product(admin, price=25.0, stock=3)
        order = buy(user, product["product_id"])

        assert other_user.status("GET", f"/orders/{order['order_id']}") == 403
        assert other_user.status("GET", f"/orders/{order['order_id']}/logistics") in (403, 404)

    def test_cannot_pay_or_cancel_another_users_order(
        self, user: Api, other_user: Api, admin: Api
    ) -> None:
        product = create_product(admin, price=25.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]

        assert other_user.status("POST", f"/orders/{order_id}/pay") == 403
        assert other_user.status("POST", f"/orders/{order_id}/cancel") == 403
        assert other_user.status("POST", f"/orders/{order_id}/confirm") == 403

        # 订单状态没被越权请求改坏
        assert order_row(order_id)["payment_status"] == "UNPAID"

    def test_cannot_refund_another_users_order(self, user: Api, other_user: Api, admin: Api) -> None:
        product = create_product(admin, price=25.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        sale = create_after_sale(user, order_id)

        resp = other_user.call("POST", f"/after-sales/{sale['after_sale_id']}/refund")
        assert resp.status_code in (403, 404), resp.text[:200]
        assert refund_count(order_id) == 0, "越权请求竟然产生了退款流水"

    def test_cannot_create_ticket_on_another_users_order(
        self, user: Api, other_user: Api, admin: Api
    ) -> None:
        product = create_product(admin, price=25.0, stock=3)
        order = buy(user, product["product_id"])

        resp = other_user.call(
            "POST", "/tickets", json={"content": "越权工单", "order_id": order["order_id"]}
        )
        assert resp.status_code == 403, resp.text[:200]

    def test_cannot_read_or_write_another_users_ticket(
        self, user: Api, other_user: Api
    ) -> None:
        ticket_id = create_ticket(user, content="我的私人工单")

        assert other_user.status("GET", f"/tickets/{ticket_id}") == 403
        assert other_user.status("POST", f"/tickets/{ticket_id}/messages", json={"content": "偷看"}) == 403
        assert other_user.status("POST", f"/tickets/{ticket_id}/process") == 403
        assert other_user.status("POST", f"/tickets/{ticket_id}/close") == 403

    def test_cart_is_per_user(self, user: Api, other_user: Api, admin: Api) -> None:
        """购物车结算只能结算自己的行 —— 传别人的 item_id 要被拒。"""
        product = create_product(admin, price=10.0, stock=5)
        user.ok("POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 1})
        my_cart = user.ok("GET", "/cart")
        my_item_id = my_cart["items"][0]["item_id"]

        other_user.ok(
            "POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 1}
        )
        resp = other_user.call("POST", "/orders", json={"item_ids": [my_item_id]})
        assert resp.status_code in (400, 403, 404), resp.text[:200]


# ======================================================================
# Tool 层：Agent 没有后门
# ======================================================================
class TestToolLayerHasNoBackdoor:
    """直接调 Tool（不经 HTTP），验证 Service 的二次校验确实存在。

    这是"Agent → Tool → Service → Repository → MySQL"里最关键的一条：
    Tool 只做参数转换，所以**把 Tool 当后门来用也退不了不该退的钱**。
    """

    def test_apply_refund_tool_denies_other_users_order(
        self, user: Api, other_user: Api, admin: Api
    ) -> None:
        from app.tools import ACTION_TOOLS

        product = create_product(admin, price=40.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")

        other_id = other_user.ok("GET", "/auth/me")["user_id"]
        result = ACTION_TOOLS["apply_refund"].invoke(
            {"order_id": order_id, "user_id": other_id, "amount": None, "reason": "越权试试",
             "approved_by": None}
        )

        assert result["success"] is False, f"Tool 竟然允许越权退款：{result}"
        assert result["error"] in ("FORBIDDEN", "PERMISSION_DENIED"), result
        assert refund_count(order_id) == 0

    def test_apply_refund_tool_cannot_exceed_order_amount(
        self, user: Api, admin: Api
    ) -> None:
        """模型说"退 1000 元"没用 —— 金额上限由 Service 说了算（v2.md §70）。"""
        from app.tools import ACTION_TOOLS

        product = create_product(admin, price=40.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        user_id = user.ok("GET", "/auth/me")["user_id"]

        result = ACTION_TOOLS["apply_refund"].invoke(
            {"order_id": order_id, "user_id": user_id, "amount": 999999.0,
             "reason": "模型瞎编的金额", "approved_by": None}
        )

        assert result["success"] is False
        assert result["error"] == "VALIDATION_ERROR", result
        assert refund_count(order_id) == 0

    def test_apply_refund_tool_rejects_unpaid_order(self, user: Api, admin: Api) -> None:
        from app.tools import ACTION_TOOLS

        product = create_product(admin, price=40.0, stock=3)
        order = buy(user, product["product_id"])  # 故意不支付
        user_id = user.ok("GET", "/auth/me")["user_id"]

        result = ACTION_TOOLS["apply_refund"].invoke(
            {"order_id": order["order_id"], "user_id": user_id, "amount": None,
             "reason": "未支付也来退", "approved_by": None}
        )

        assert result["success"] is False
        assert result["error"] in ("REFUND_NOT_ALLOWED", "INVALID_STATE"), result

    def test_query_tools_do_not_leak_other_users_data(
        self, user: Api, other_user: Api, admin: Api
    ) -> None:
        """只读工具同样要按"当前用户"过滤 —— 否则模型能读到别人的订单。"""
        from app.tools import ACTION_TOOLS, QUERY_TOOLS

        product = create_product(admin, price=15.0, stock=3)
        order = buy(user, product["product_id"])
        other_id = other_user.ok("GET", "/auth/me")["user_id"]

        # query_order：订单归属校验
        tool = QUERY_TOOLS.get("query_order")
        if tool is not None:
            result = tool.invoke({"order_id": order["order_id"], "user_id": other_id})
            assert result.get("success") is False, f"只读工具泄露了他人订单：{result}"

    def test_check_refund_eligibility_is_read_only(self, user: Api, admin: Api) -> None:
        """评估工具绝不能动业务 —— 它会被模型反复调用。"""
        from app.tools import ALL_TOOLS

        product = create_product(admin, price=40.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        user_id = user.ok("GET", "/auth/me")["user_id"]

        # 注意：它不在 QUERY_TOOLS 里（那个字典是"按决策开关批量调用"的清单），
        # 而是单独注册在 ALL_TOOLS —— 模型可以主动调它做退款前预判。
        tool = ALL_TOOLS.get("check_refund_eligibility")
        assert tool is not None, "缺少 check_refund_eligibility 工具"

        before = order_row(order_id)
        result = tool.invoke({"order_id": order_id, "user_id": user_id, "amount": None})
        after = order_row(order_id)

        assert result.get("success") is True, result
        assert before == after, "评估工具改了订单状态"
        assert refund_count(order_id) == 0, "评估工具竟然产生了退款流水"

    def test_eligibility_tool_does_not_approve_high_amount_refund(
        self, user: Api, admin: Api
    ) -> None:
        """超过自动退款上限：评估工具要如实说"需要人工"，并且**不执行**退款。"""
        from app.core.config import settings
        from app.tools import ALL_TOOLS

        limit = float(settings.refund_auto_approve_limit)
        product = create_product(admin, price=limit + 100, stock=2)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        user_id = user.ok("GET", "/auth/me")["user_id"]

        result = ALL_TOOLS["check_refund_eligibility"].invoke(
            {"order_id": order_id, "user_id": user_id, "amount": None}
        )

        assert result["success"] is True, result
        assert result["data"]["need_human"] is True, f"超额退款竟然不需要人工：{result}"
        assert refund_count(order_id) == 0


# ======================================================================
# 状态机：非法状态转换一律被拒
# ======================================================================
class TestIllegalStateTransitions:
    def test_cannot_ship_unpaid_order(self, user: Api, staff: Api, admin: Api) -> None:
        product = create_product(admin, price=20.0, stock=3)
        order = buy(user, product["product_id"])
        assert staff.status("POST", f"/customer-service/orders/{order['order_id']}/ship") == 409

    def test_cannot_confirm_before_shipped(self, user: Api, admin: Api) -> None:
        product = create_product(admin, price=20.0, stock=3)
        order = buy(user, product["product_id"])
        user.ok("POST", f"/orders/{order['order_id']}/pay")
        assert user.status("POST", f"/orders/{order['order_id']}/confirm") == 409

    def test_cannot_cancel_paid_order(self, user: Api, admin: Api) -> None:
        product = create_product(admin, price=20.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")

        resp = user.call("POST", f"/orders/{order_id}/cancel")
        assert resp.status_code == 409
        assert order_row(order_id)["fulfillment_status"] != "CANCELLED"

    def test_cannot_refund_unpaid_order_via_api(self, user: Api, admin: Api) -> None:
        """未支付订单走退款链路：**在售后申请这一步就被拒**（比退款更早更省的拦截）。

        v2.md §17 的第 3 条规则是"订单已经支付"，所以"未支付"根本走不到退款：
        `POST /after-sales` 就会返回 409。退款服务里的 `NOT_PAID` 分支
        是给"售后单已存在、但订单状态后来被改动"这种边界兜底的。
        """
        product = create_product(admin, price=20.0, stock=3)
        order = buy(user, product["product_id"])

        resp = user.call(
            "POST",
            "/after-sales",
            json={"order_id": order["order_id"], "type": "REFUND", "reason": "未支付也来退"},
        )
        assert resp.status_code == 409, resp.text[:200]
        assert resp.json()["code"] == "INVALID_STATE"
        assert refund_count(order["order_id"]) == 0

    def test_refund_service_rejects_unpaid_order(self, user: Api, admin: Api) -> None:
        """绕过售后申请、直接调 Service：未支付照样退不了（第二道校验）。"""
        from app.core.exceptions import RefundNotAllowedError
        from app.services import refund_service

        product = create_product(admin, price=20.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user_id = user.ok("GET", "/auth/me")["user_id"]

        with pytest.raises(RefundNotAllowedError):
            refund_service.apply_refund(order_id=order_id, user_id=user_id)
        assert refund_count(order_id) == 0


# ======================================================================
# 金额校验（Service 层，不是 Tool 层）
# ======================================================================
class TestAmountValidation:
    def test_refund_amount_cannot_exceed_order_total(self, user: Api, admin: Api) -> None:
        from app.services import refund_service
        from app.core.exceptions import ValidationError

        product = create_product(admin, price=50.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        user_id = user.ok("GET", "/auth/me")["user_id"]

        with pytest.raises(ValidationError):
            refund_service.apply_refund(
                order_id=order_id, user_id=user_id, amount=Decimal("999999")
            )
        assert refund_count(order_id) == 0

    def test_refund_amount_must_be_positive(self, user: Api, admin: Api) -> None:
        from app.core.exceptions import ValidationError
        from app.services import refund_service

        product = create_product(admin, price=50.0, stock=3)
        order = buy(user, product["product_id"])
        order_id = order["order_id"]
        user.ok("POST", f"/orders/{order_id}/pay")
        user_id = user.ok("GET", "/auth/me")["user_id"]

        with pytest.raises(ValidationError):
            refund_service.apply_refund(
                order_id=order_id, user_id=user_id, amount=Decimal("-1")
            )
