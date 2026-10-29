"""安全审查回归：认证、交易一致性、真正同时起跑的并发请求及图状态隔离。"""
from contextlib import nullcontext
from unittest.mock import MagicMock

import asyncio
import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.state import AgentState
from app.core import dependencies
from app.core.config import Settings
from app.core.exceptions import InvalidStateError, PermissionDeniedError, ValidationError
from app.services import agent_service, refund_service, ticket_service, knowledge_service
from tests.conftest import buy, create_after_sale, create_product, concurrent, order_row, product_stock, refund_count


@pytest.mark.parametrize("subject", [None, "bad", "0", "-2", [], {}])
def test_invalid_token_subject_returns_401(monkeypatch, subject):
    monkeypatch.setattr(dependencies, "decode_access_token", lambda _: {"sub": subject})
    with pytest.raises(HTTPException) as error:
        dependencies.get_current_user(credentials=None, token="test")
    assert error.value.status_code == 401


def test_disabled_user_cannot_use_existing_token(monkeypatch):
    monkeypatch.setattr(dependencies, "decode_access_token", lambda _: {"sub": "1"})
    monkeypatch.setattr(dependencies, "session_scope", lambda: nullcontext(None))
    monkeypatch.setattr(dependencies.user_repository, "get_by_id", lambda *a: {"status": "DISABLED"})
    with pytest.raises(HTTPException) as error:
        dependencies.get_current_user(credentials=None, token="old-token")
    assert error.value.status_code == 401


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), "wrong", -1, 101, 50])
def test_refund_rejects_invalid_or_partial_amount(amount):
    with pytest.raises(ValidationError):
        refund_service._resolve_amount({"total_amount": 100}, amount)


def test_production_rejects_default_secret_and_stub():
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None, app_env="production", jwt_secret="dev-only-change-me")
    with pytest.raises(ValueError, match="AGENT_STUB_LLM"):
        Settings(_env_file=None, app_env="production", jwt_secret="x" * 40, agent_stub_llm=True)


@pytest.mark.asyncio
async def test_fresh_invocation_clears_approval_and_does_not_duplicate_messages(monkeypatch):
    messages = [{"content": "咨询"}]
    monkeypatch.setattr(agent_service, "_recent_messages", lambda _: messages)
    initial = agent_service._build_initial_state(ticket={"id": 1, "user_id": 1},
        agent_run_id=1, thread_id="reset", content="咨询")
    builder = StateGraph(AgentState)
    builder.add_node("noop", lambda state: {})
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "reset"}}
    await graph.ainvoke({**initial, "human_approved": True, "human_operator_id": 9,
                         "action_result": {"executed": True}}, config)
    final = await graph.ainvoke(initial, config)
    assert final["human_approved"] is None and final["human_operator_id"] is None
    assert final["action_result"] is None and final["conversation"] == messages


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "1"])
async def test_sse_replayed_done_closes_stream(monkeypatch, cursor):
    from app.api import tickets
    from app.agent.progress import ProgressBus
    bus = ProgressBus()
    bus.publish(1, {"type": "done", "status": "COMPLETED"})
    monkeypatch.setattr(tickets, "progress_bus", bus)
    monkeypatch.setattr(ticket_service, "get_ticket_raw", lambda *a, **kw: {})
    request = MagicMock()
    response = await tickets.stream_ticket(1, request, last_event_id=cursor,
                                          current_user={"id": 1, "role": "USER"})
    frames = await asyncio.wait_for(_frames(response), timeout=1)
    if cursor is None:
        assert len(frames) == 1 and "event: done" in frames[0]
    else:
        assert frames == []
    assert bus.subscriber_count(1) == 0


async def _frames(response):
    return [frame async for frame in response.body_iterator]


def test_same_cart_concurrent_checkout_creates_one_order(user, admin):
    product = create_product(admin, stock=30)
    user.ok("POST", "/cart/items", json={"product_id": product["product_id"], "quantity": 1})
    responses = concurrent("POST", user.base_url, "/orders", user.token, 8, json_body={})
    assert sum(response.status_code in (200, 201) for response in responses) == 1
    assert all(response.status_code in (200, 201, 400, 409) for response in responses)
    assert product_stock(product["product_id"]) == 29
    assert user.ok("GET", "/orders")["total"] == 1


def test_concurrent_cart_add_does_not_lose_quantity(user, admin):
    product = create_product(admin, stock=30)
    responses = concurrent("POST", user.base_url, "/cart/items", user.token, 8,
                           json_body={"product_id": product["product_id"], "quantity": 1})
    assert all(response.status_code in (200, 201) for response in responses)
    assert user.ok("GET", "/cart")["items"][0]["quantity"] == 8


def test_refund_rechecks_shipping_after_evaluation(user, staff, admin, monkeypatch):
    product = create_product(admin)
    order_id = buy(user, product["product_id"])["order_id"]
    user.ok("POST", f"/orders/{order_id}/pay")
    user_id = user.ok("GET", "/auth/me")["user_id"]
    evaluate = refund_service.evaluate_refund
    def ship_after_evaluation(**kwargs):
        result = evaluate(**kwargs)
        staff.ok("POST", f"/customer-service/orders/{order_id}/ship")
        return result
    monkeypatch.setattr(refund_service, "evaluate_refund", ship_after_evaluation)
    outcome = refund_service.apply_refund(order_id=order_id, user_id=user_id)
    assert not outcome["executed"] and outcome["need_human"]
    assert refund_count(order_id) == 0 and product_stock(product["product_id"]) == 9
    assert order_row(order_id)["payment_status"] == "PAID"


def test_normal_user_cannot_be_refund_approver(user, admin):
    product = create_product(admin, price=800)
    order_id = buy(user, product["product_id"])["order_id"]
    user.ok("POST", f"/orders/{order_id}/pay")
    user_id = user.ok("GET", "/auth/me")["user_id"]
    with pytest.raises(PermissionDeniedError):
        refund_service.apply_refund(order_id=order_id, user_id=user_id, approved_by=user_id)
    assert refund_count(order_id) == 0 and order_row(order_id)["payment_status"] == "PAID"


def test_ticket_and_refund_cannot_link_another_orders_after_sale(user, admin):
    product = create_product(admin)
    orders = [buy(user, product["product_id"])["order_id"] for _ in range(2)]
    for order_id in orders:
        user.ok("POST", f"/orders/{order_id}/pay")
    sale = create_after_sale(user, orders[0])
    sale_id = sale["after_sale_id"]
    user_id = user.ok("GET", "/auth/me")["user_id"]
    with pytest.raises(InvalidStateError):
        ticket_service.create_ticket(user_id=user_id, content="售后", order_id=orders[1], after_sale_id=sale_id)
    with pytest.raises(PermissionDeniedError):
        refund_service.apply_refund(order_id=orders[1], user_id=user_id, after_sale_id=sale_id)
    assert refund_count(orders[1]) == 0
    ticket = ticket_service.create_ticket(user_id=user_id, content="售后", after_sale_id=sale_id)
    assert ticket["order_id"] == orders[0]


def test_failed_knowledge_publish_remains_draft(db_or_skip, monkeypatch):
    from app.core.database import session_scope
    from app.repositories import knowledge_repository
    from sqlalchemy import select
    from app.models.knowledge import KnowledgeDocument
    def fail(*a, **kw):
        raise RuntimeError("embedding unavailable")
    monkeypatch.setattr(knowledge_service, "_build_chunks", fail)
    with pytest.raises(RuntimeError):
        knowledge_service.create_document(title="pytest_failed_publish", content="售后规则", publish=True)
    with session_scope() as db:
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.title == "pytest_failed_publish"))
        assert document.status == "DRAFT"


def test_publish_does_not_override_concurrent_edit(db_or_skip, monkeypatch):
    from app.core.database import session_scope
    from app.repositories import knowledge_repository
    document = knowledge_service.create_document(title="pytest_edit", content="原规则")
    def edit(*a, **kw):
        with session_scope() as db:
            knowledge_repository.update_document(db, document["id"], content="新规则")
        return 1
    monkeypatch.setattr(knowledge_service, "_build_chunks", edit)
    with pytest.raises(ValidationError):
        knowledge_service.publish_document(document["id"])
    assert knowledge_service.get_document(document["id"])["status"] == "DRAFT"


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", [False, True])
async def test_agent_cancel_order_executes_service_and_respects_payment(user, admin, paid):
    from app.agent.nodes.action import execute_action, _cancel_with_idempotency_guard
    product = create_product(admin)
    order_id = buy(user, product["product_id"])["order_id"]
    if paid:
        user.ok("POST", f"/orders/{order_id}/pay")
    user_id = user.ok("GET", "/auth/me")["user_id"]
    ticket = ticket_service.create_ticket(user_id=user_id, content="请取消订单", order_id=order_id)
    state = {"ticket_id": ticket["id"], "user_id": user_id, "order_id": order_id,
             "business_action": "cancel_order", "result": {"solution": "取消订单"}}
    result = await execute_action(state)
    if paid:
        assert result["status"] == "WAITING_HUMAN"
        assert order_row(order_id)["fulfillment_status"] != "CANCELLED"
        assert product_stock(product["product_id"]) == 9
    else:
        assert result["status"] == "COMPLETED"
        assert order_row(order_id)["fulfillment_status"] == "CANCELLED"
        assert product_stock(product["product_id"]) == 10
        assert _cancel_with_idempotency_guard(state)["duplicate"]
        assert product_stock(product["product_id"]) == 10


def test_refund_and_after_sale_cancel_start_simultaneously(user, admin):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.core.exceptions import BusinessError
    from app.services import after_sale_service
    from tests.conftest import query_one
    product = create_product(admin)
    order_id = buy(user, product["product_id"])["order_id"]
    user.ok("POST", f"/orders/{order_id}/pay")
    sale_id = create_after_sale(user, order_id)["after_sale_id"]
    user_id = user.ok("GET", "/auth/me")["user_id"]
    barrier = threading.Barrier(2)
    def refund():
        barrier.wait(timeout=5)
        return refund_service.apply_refund(order_id=order_id, user_id=user_id)
    def cancel():
        barrier.wait(timeout=5)
        try:
            return after_sale_service.cancel_after_sale(after_sale_id=sale_id, user_id=user_id)
        except BusinessError as exc:
            return exc.to_dict()
    with ThreadPoolExecutor(max_workers=2) as pool:
        refund_task, cancel_task = pool.submit(refund), pool.submit(cancel)
        assert refund_task.result(timeout=15)["executed"]
        cancel_task.result(timeout=15)
    assert refund_count(order_id, status="SUCCESS") == 1
    assert order_row(order_id)["payment_status"] == "REFUNDED"
    assert product_stock(product["product_id"]) == 10
    assert query_one("SELECT COUNT(*) AS n FROM after_sales WHERE order_id=%s AND status='COMPLETED'", (order_id,))["n"] == 1
