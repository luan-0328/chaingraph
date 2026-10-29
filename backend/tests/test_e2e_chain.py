"""完整链路：下单 → 支付 → 售后 → 工单 → Agent → **真的退款** → 结单（v2.md §5 / §38）。

这是唯一会把钱真的退掉的测试文件 —— 它跑在 `chain_server`（:8023）上，
那个实例的 LLM 桩把 `business_action` 设成 `apply_refund`，
于是 Agent 的最终决策会真的走完 Tool → RefundService → Repository → MySQL。

它回答的问题不是"某个接口通不通"，而是：

    整条业务闭环能不能一次跑通？
    钱退完之后各个状态是否自洽（订单/售后/库存/流水/工单/幂等记录）？
    链路跑完之后**重复触发**会不会退第二次？

同时覆盖知识库发布/下线对 RAG 的实际影响（需求 ⑦）：
未发布的知识绝不能被 Agent 检索到。
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    Api,
    Server,
    buy,
    create_after_sale,
    create_product,
    create_ticket,
    order_row,
    payment_count,
    product_stock,
    query_all,
    query_one,
    refund_count,
    run_event_messages,
    runs_of,
    ticket_row,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def chain_user(chain_server: Server) -> Api:
    api = Api(chain_server.base_url)
    api.register_and_login("pytest_chain")
    yield api
    api.close()


@pytest.fixture
def chain_admin(chain_server: Server) -> Api:
    api = Api(chain_server.base_url)
    api.login(username="admin", password="admin123")
    yield api
    api.close()


@pytest.fixture
def chain_staff(chain_server: Server) -> Api:
    api = Api(chain_server.base_url)
    api.login(username="service01", password="123456")
    yield api
    api.close()


# ======================================================================
# 主链路
# ======================================================================
class TestFullChain:
    """一条链路从头走到尾，逐步断言钱与状态。"""

    def test_order_to_refund_chain(self, chain_server: Server, chain_user: Api, chain_admin: Api) -> None:
        # ---- ① 商品 ----
        product = create_product(chain_admin, price=120.0, stock=10)
        product_id = product["product_id"]

        # ---- ② 下单（扣库存）----
        order = buy(chain_user, product_id, quantity=2)
        order_id = order["order_id"]
        assert float(order["total_amount"]) == 240.0
        assert product_stock(product_id) == 8, "下单没有扣库存"

        # ---- ③ 支付 ----
        chain_user.ok("POST", f"/orders/{order_id}/pay")
        assert order_row(order_id)["payment_status"] == "PAID"
        assert payment_count(order_id, status="SUCCESS") == 1

        # ---- ④ 售后单 ----
        sale = create_after_sale(chain_user, order_id, reason="不想要了")
        assert sale["status"] in ("APPLYING", "PROCESSING")

        # ---- ⑤ 工单（带订单上下文）----
        ticket_id = create_ticket(
            chain_user, content="我刚刚买的商品不想要了，帮我退款", order_id=order_id
        )
        row = ticket_row(ticket_id)
        assert row["status"] in ("OPEN", "PROCESSING")

        # 工单首条消息 = 用户原话（TicketMessage 统一消息流）
        detail = chain_user.ok("GET", f"/tickets/{ticket_id}")
        assert detail["messages"][0]["sender_type"] == "USER"
        assert detail["order_id"] == order_id, "工单没有关联订单上下文"

        # ---- ⑥ Agent 处理（真的会退钱）----
        payload = chain_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "COMPLETED", payload

        run = runs_of(ticket_id)[0]
        assert run["status"] == "COMPLETED"
        assert run["used_rag"] in (1, True), "Agent 没有检索知识库"
        assert run["current_node"] is None, "跑完之后 current_node 没有清空"

        # ---- ⑦ 钱真的退了，而且只退了一次 ----
        assert order_row(order_id)["payment_status"] == "REFUNDED", "Agent 没有真的退款"
        assert refund_count(order_id, status="SUCCESS") == 1, "退款流水不是恰好一笔"

        # 售后单结单
        after_sale = query_one(
            "SELECT status FROM after_sales WHERE order_id=%s ORDER BY id DESC LIMIT 1",
            (order_id,),
        )
        assert after_sale is not None and after_sale["status"] == "COMPLETED"

        # 未发货退款 → 库存回补
        assert product_stock(product_id) == 10, "退款没有回补库存（或回补次数不对）"

        # ---- ⑧ 幂等记录留下 ----
        claims = query_all(
            "SELECT dedup_key, result FROM side_effect_claims WHERE ticket_id=%s", (ticket_id,)
        )
        assert len(claims) == 1, f"幂等键记录不对：{claims}"
        assert "refund" in claims[0]["dedup_key"]

        # ---- ⑨ 工单收口 ----
        assert ticket_row(ticket_id)["agent_status"] == "IDLE"
        resolved = chain_user.ok("GET", f"/tickets/{ticket_id}")
        assert resolved["status"] in ("RESOLVED", "CLOSED"), resolved["status"]

        # Agent 的结论进了统一消息流
        agent_messages = [m for m in resolved["messages"] if m["sender_type"] == "AGENT"]
        assert agent_messages, "Agent 的回复没有写进工单消息"

        # 过程事件齐全
        event_types = [
            r["event_type"]
            for r in query_all(
                "SELECT event_type FROM agent_run_events WHERE agent_run_id=%s ORDER BY id",
                (run["id"],),
            )
        ]
        assert event_types[0] == "STARTED"
        assert "EXECUTING_REFUND" in event_types, event_types
        assert "COMPLETED" in event_types, event_types

    def test_rerunning_agent_after_chain_does_not_refund_again(
        self, chain_server: Server, chain_user: Api, chain_admin: Api
    ) -> None:
        """链路跑完再点一次"处理"：绝不能产生第二笔退款。"""
        product = create_product(chain_admin, price=80.0, stock=5)
        order = buy(chain_user, product["product_id"])
        order_id = order["order_id"]
        chain_user.ok("POST", f"/orders/{order_id}/pay")
        ticket_id = create_ticket(chain_user, content="帮我退款", order_id=order_id)

        chain_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert refund_count(order_id) == 1

        # 再点一次（无论成功与否，都不能退第二笔）
        chain_user.call("POST", f"/tickets/{ticket_id}/process")
        assert refund_count(order_id) == 1, "重复处理产生了第二笔退款"
        assert refund_count(order_id, status="SUCCESS") == 1

        # Agent 应该如实说"已经退过了"
        runs = runs_of(ticket_id)
        messages = run_event_messages([r["id"] for r in runs], "退款")
        assert messages, "没有任何退款相关事件"

    def test_customer_service_can_review_the_whole_process(
        self, chain_server: Server, chain_user: Api, chain_admin: Api, chain_staff: Api
    ) -> None:
        """客服工作台能复盘：Agent 决策、结果、事件、执行记录全都能看到。"""
        product = create_product(chain_admin, price=70.0, stock=5)
        order = buy(chain_user, product["product_id"])
        chain_user.ok("POST", f"/orders/{order['order_id']}/pay")
        ticket_id = create_ticket(chain_user, content="帮我退款", order_id=order["order_id"])
        chain_user.ok("POST", f"/tickets/{ticket_id}/process")

        detail = chain_staff.ok("GET", f"/customer-service/tickets/{ticket_id}")

        assert detail["agent_run"] is not None
        assert detail["agent_run"]["status"] == "COMPLETED"
        assert detail["agent_run"]["result"], "看不到 Agent 的结构化结果"
        assert detail["agent_events"], "看不到 Agent 执行事件"
        assert detail["agent_runs"], "看不到历史执行记录"
        assert detail["order_id"] == order["order_id"], "工单没有带出订单上下文"

    def test_refund_flow_via_proactive_after_sale(
        self, chain_server: Server, chain_user: Api, chain_admin: Api
    ) -> None:
        """用户自己先申请售后、再退款：同样只退一次（走 HTTP 路径而非 Agent）。"""
        product = create_product(chain_admin, price=90.0, stock=4)
        order = buy(chain_user, product["product_id"])
        order_id = order["order_id"]
        chain_user.ok("POST", f"/orders/{order_id}/pay")
        sale = create_after_sale(chain_user, order_id)

        result = chain_user.ok("POST", f"/after-sales/{sale['after_sale_id']}/refund")
        assert result["executed"] is True
        assert result["need_human"] is False

        assert order_row(order_id)["payment_status"] == "REFUNDED"
        assert refund_count(order_id, status="SUCCESS") == 1


# ======================================================================
# 人工审核后由 Agent 完成退款（HITL 真恢复 + 真退钱）
# ======================================================================
class TestHitlRefundChain:
    """已发货订单 → Agent 判转人工 → 客服批准 → **恢复原图并真的退款**。"""

    def test_business_limit_fallback_is_approved_and_refunded(
        self, chain_user: Api, chain_admin: Api, chain_staff: Api
    ) -> None:
        # 桩模型认为可自动处理，后端金额规则必须真正挂起图。
        product = create_product(chain_admin, price=1000.0, stock=3)
        order = buy(chain_user, product["product_id"])
        order_id = order["order_id"]
        chain_user.ok("POST", f"/orders/{order_id}/pay")
        ticket_id = create_ticket(chain_user, content="申请退款", order_id=order_id)
        waiting = chain_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert waiting["status"] == "WAITING_HUMAN"
        assert waiting["need_human"] is True
        assert refund_count(order_id) == 0
        original = runs_of(ticket_id)[0]
        chain_staff.ok("POST", f"/tickets/{ticket_id}/approve")
        assert refund_count(order_id, status="SUCCESS") == 1
        assert order_row(order_id)["payment_status"] == "REFUNDED"
        assert ticket_row(ticket_id)["agent_status"] == "IDLE"
        assert ticket_row(ticket_id)["status"] == "RESOLVED"
        runs = runs_of(ticket_id)
        assert len(runs) == 1
        assert runs[0]["id"] == original["id"]
        assert runs[0]["status"] == "COMPLETED"

    def test_approved_refund_executes_and_records_approver(
        self, chain_server: Server, chain_user: Api, chain_admin: Api, chain_staff: Api
    ) -> None:
        from tests.conftest import stub_registry

        product = create_product(chain_admin, price=200.0, stock=3)
        order = buy(chain_user, product["product_id"])
        order_id = order["order_id"]
        chain_user.ok("POST", f"/orders/{order_id}/pay")

        ticket_id = create_ticket(
            chain_user, content="已发货了但我想退款", order_id=order_id, type="REFUND"
        )
        stub_registry(chain_server, "human", ticket_id)

        payload = chain_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "WAITING_HUMAN"
        original = runs_of(ticket_id)[0]
        assert refund_count(order_id) == 0, "等待人工期间不该已经退款"

        # ---- 客服批准：恢复原图，真正执行退款 ----
        staff_id = chain_staff.ok("GET", "/auth/me")["user_id"]
        chain_staff.ok("POST", f"/tickets/{ticket_id}/approve", json={"comment": "同意退款"})

        runs = runs_of(ticket_id)
        assert len(runs) == 1, f"批准不该新建 run：{runs}"
        assert runs[0]["id"] == original["id"]
        assert runs[0]["thread_id"] == original["thread_id"], "恢复的不是原来的图"
        assert runs[0]["status"] == "COMPLETED"

        assert order_row(order_id)["payment_status"] == "REFUNDED", "批准后没有真的退款"

        record = query_one(
            "SELECT approved_by, status FROM refund_records WHERE order_id=%s ORDER BY id DESC LIMIT 1",
            (order_id,),
        )
        assert record is not None
        assert record["status"] == "SUCCESS"
        assert record["approved_by"] == staff_id, "退款流水没有记录审批人"

        assert ticket_row(ticket_id)["agent_status"] == "IDLE"

    def test_rejected_hitl_never_refunds(
        self, chain_server: Server, chain_user: Api, chain_admin: Api, chain_staff: Api
    ) -> None:
        from tests.conftest import stub_registry

        product = create_product(chain_admin, price=200.0, stock=3)
        order = buy(chain_user, product["product_id"])
        order_id = order["order_id"]
        chain_user.ok("POST", f"/orders/{order_id}/pay")

        ticket_id = create_ticket(
            chain_user, content="想退款但大概率不符合", order_id=order_id, type="REFUND"
        )
        stub_registry(chain_server, "human", ticket_id)
        chain_user.ok("POST", f"/tickets/{ticket_id}/process")

        chain_staff.ok("POST", f"/tickets/{ticket_id}/reject", json={"reason": "不符合规则"})

        assert refund_count(order_id) == 0, "驳回竟然退了款"
        assert order_row(order_id)["payment_status"] == "PAID"
        assert runs_of(ticket_id)[0]["status"] == "REJECTED"


# ======================================================================
# 知识库发布 / 下线 对 RAG 的实际影响（需求 ⑦）
# ======================================================================
class TestKnowledgeLifecycle:
    """知识的"发布"与"下线"必须真的改变 Agent 能检索到什么。"""

    def test_draft_is_not_retrievable_until_published(
        self, chain_server: Server, chain_admin: Api
    ) -> None:
        """草稿 → 发布 → 下线，每一步都要真的改变"Agent 能检索到什么"。"""
        # 查询词刻意选成正文里连续出现的一段：
        # 默认的本地降级向量器是**字符 n-gram 哈希**（词法相似度），
        # 用"只在正文里零散出现"的词去查会命中不稳 —— 那不是被测行为，是向量器局限。
        marker = "极速退款专用标记词"
        content = f"# pytest 知识\n\n{marker} 的处理规则是：必须人工复核后才能执行。"

        created = chain_admin.ok(
            "POST",
            "/admin/knowledge",
            json={"title": "pytest 生命周期测试文档", "content": content, "category": "pytest"},
        )
        document_id = created["document_id"]

        # 新建 = 草稿，检索不到
        assert created["status"] == "DRAFT"
        assert created["chunk_count"] == 0

        hits = chain_admin.ok("GET", f"/admin/knowledge/search?query={marker}")["hits"]
        assert not any(marker in hit["content"] for hit in hits), "草稿知识被检索到了"

        # ---- 发布：切分 → Embedding → 写入向量库 ----
        published = chain_admin.ok("POST", f"/admin/knowledge/{document_id}/publish")
        assert published["status"] == "ENABLED"
        assert published["chunk_count"] > 0, "发布没有生成任何分块"

        chunks = query_all(
            "SELECT id, embedding FROM knowledge_chunks WHERE document_id=%s", (document_id,)
        )
        assert chunks, "发布没有把分块写进向量库"
        assert all(chunk["embedding"] is not None for chunk in chunks), "有分块没有向量"

        hits = chain_admin.ok("GET", f"/admin/knowledge/search?query={marker}")["hits"]
        assert any(marker in hit["content"] for hit in hits), "发布之后仍然检索不到"

        # ---- 下线：立刻检索不到，但分块保留 ----
        offline = chain_admin.ok("POST", f"/admin/knowledge/{document_id}/offline")
        assert offline["status"] == "DISABLED"

        hits = chain_admin.ok("GET", f"/admin/knowledge/search?query={marker}")["hits"]
        assert not any(marker in hit["content"] for hit in hits), "下线之后仍然能被检索到"

        still_there = query_all(
            "SELECT id FROM knowledge_chunks WHERE document_id=%s", (document_id,)
        )
        assert still_there, "下线把分块删了（重新发布又要重算向量）"

        # 清理
        assert chain_admin.status("DELETE", f"/admin/knowledge/{document_id}") == 204

    def test_editing_content_returns_to_draft_and_invalidates_vectors(
        self, chain_server: Server, chain_admin: Api
    ) -> None:
        """改了正文 → 退回草稿 + 旧向量作废（否则会拿过时知识回答用户）。"""
        created = chain_admin.ok(
            "POST",
            "/admin/knowledge",
            json={"title": "pytest 改稿测试", "content": "# 改稿\n\n原始内容：可以退款。", "category": "pytest"},
        )
        document_id = created["document_id"]
        chain_admin.ok("POST", f"/admin/knowledge/{document_id}/publish")

        before = query_one(
            "SELECT version FROM knowledge_documents WHERE id=%s", (document_id,)
        )
        updated = chain_admin.ok(
            "PATCH",
            f"/admin/knowledge/{document_id}",
            json={"content": "# 改稿\n\n新内容：不可以退款了。"},
        )

        assert updated["status"] == "DRAFT", "改了正文却没有退回草稿"
        assert updated["chunk_count"] == 0, "改了正文却还留着旧分块（会检索到过时知识）"
        assert updated["version"] != before["version"], "版本号没有递增（知识变更无留痕）"

        chunks = query_all(
            "SELECT id FROM knowledge_chunks WHERE document_id=%s", (document_id,)
        )
        assert not chunks, "旧向量没有被作废"

        chain_admin.status("DELETE", f"/admin/knowledge/{document_id}")

    def test_deleting_document_removes_its_chunks(
        self, chain_server: Server, chain_admin: Api
    ) -> None:
        """删除文档要连带删掉分块 —— 否则向量库里留下永远被检索到的孤儿。"""
        created = chain_admin.ok(
            "POST",
            "/admin/knowledge",
            json={
                "title": "pytest 删除测试",
                "content": "# 待删知识\n\n删除测试专用内容。",
                "category": "pytest",
                "publish": True,
            },
        )
        document_id = created["document_id"]
        assert created["chunk_count"] > 0

        assert chain_admin.status("DELETE", f"/admin/knowledge/{document_id}") == 204

        orphans = query_all(
            "SELECT id FROM knowledge_chunks WHERE document_id=%s", (document_id,)
        )
        assert not orphans, "文档已删除但分块还在（向量库出现孤儿）"

    def test_knowledge_stats_is_consistent(self, chain_server: Server, chain_admin: Api) -> None:
        stats = chain_admin.ok("GET", "/admin/knowledge/stats")
        assert stats["chunks"]["total"] >= stats["chunks"]["indexed"]
        assert "DRAFT" in stats["documents"]
        assert "ENABLED" in stats["documents"]
        assert "DISABLED" in stats["documents"]
