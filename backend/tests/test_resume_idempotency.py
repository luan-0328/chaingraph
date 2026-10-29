"""持久化副作用的真实并发去重，以及审批后故障的真实 LangGraph 续跑。"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import nodes
from app.agent.events import emit_event
from app.agent.graph import build_graph
from app.agent.progress import progress_bus
from app.core.database import session_scope
from app.core.exceptions import AgentExecutionError
from app.repositories import agent_run_repository
from app.services import agent_service, ticket_service
from tests.conftest import buy, create_product, query_one, query_all, product_stock, refund_count


def setup_ticket(user):
    uid = user.ok("GET", "/auth/me")["user_id"]
    ticket = ticket_service.create_ticket(user_id=uid, content="人工审核测试")
    with session_scope() as db:
        run = agent_run_repository.create_run(db, user_id=uid, ticket_id=ticket["id"],
                                              thread_id=f"dedup-{ticket['id']}")
    return uid, ticket["id"], run["id"]


@pytest.mark.parametrize("batch", range(3))
def test_concurrent_event_and_message_insert_once(user, batch):
    _, tid, rid = setup_ticket(user)
    barrier = threading.Barrier(6)
    def insert(_):
        barrier.wait(timeout=5)
        emit_event(ticket_id=tid, agent_run_id=rid, event_type="WAITING_HUMAN",
                   message="待审核", dedup_key=f"run:{rid}:review:1:waiting")
        return ticket_service.add_message(ticket_id=tid, content="等候人工", sender_type="AGENT",
                                          dedup_key=f"run:{rid}:waiting:1")
    with ThreadPoolExecutor(max_workers=6) as pool:
        messages = list(pool.map(insert, range(6)))
    assert len({message["id"] for message in messages}) == 1
    assert query_one("SELECT COUNT(*) AS n FROM agent_run_events WHERE agent_run_id=%s", (rid,))["n"] == 1
    assert len(progress_bus.history_since(tid, None)) == 1
    # 同一条内容在下一审批轮次是真实新事件，不能一并吞掉。
    emit_event(ticket_id=tid, agent_run_id=rid, event_type="WAITING_HUMAN",
               message="待审核", dedup_key=f"run:{rid}:review:2:waiting")
    assert query_one("SELECT COUNT(*) AS n FROM agent_run_events WHERE agent_run_id=%s", (rid,))["n"] == 2


def test_resolution_retry_does_not_reclose_reopened_ticket(user):
    uid, tid, rid = setup_ticket(user)
    ticket_service.resolve_ticket(ticket_id=tid, solution="完成", agent_run_id=rid, agent_reply=True)
    ticket_service.reopen_ticket(ticket_id=tid, user_id=uid)
    result = ticket_service.resolve_ticket(ticket_id=tid, solution="重试文本", agent_run_id=rid, agent_reply=True)
    assert result["status"] == "PROCESSING"
    assert query_one("SELECT COUNT(*) AS n FROM ticket_messages WHERE ticket_id=%s AND dedup_key IS NOT NULL", (tid,))["n"] == 2


def test_reply_and_resolution_rollback_together(user, monkeypatch):
    from app.repositories import ticket_repository
    uid, tid, rid = setup_ticket(user)
    original = ticket_repository.add_message
    def fail_system_message(*args, **kwargs):
        if kwargs.get("sender_type") == "SYSTEM":
            raise RuntimeError("system message failed")
        return original(*args, **kwargs)
    monkeypatch.setattr(ticket_repository, "add_message", fail_system_message)
    with pytest.raises(RuntimeError):
        ticket_service.resolve_ticket(ticket_id=tid, solution="完成", agent_run_id=rid, agent_reply=True)
    assert ticket_service.get_ticket_raw(tid)["status"] == "OPEN"
    assert query_one("SELECT COUNT(*) AS n FROM ticket_messages WHERE ticket_id=%s AND sender_type='AGENT'", (tid,))["n"] == 0
    assert query_one("SELECT COUNT(*) AS n FROM user_memories WHERE source=%s", (f"ticket:{tid}",))["n"] == 0
    monkeypatch.setattr(ticket_repository, "add_message", original)
    ticket_service.resolve_ticket(ticket_id=tid, solution="完成", agent_run_id=rid, agent_reply=True)
    assert query_one("SELECT COUNT(*) AS n FROM ticket_messages WHERE ticket_id=%s AND dedup_key IS NOT NULL", (tid,))["n"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["after_refund", "after_resolution", "after_checkpoint"])
async def test_approved_resume_survives_failure_without_duplicate_effects(user, admin, staff, monkeypatch, fault):
    uid = user.ok("GET", "/auth/me")["user_id"]
    operator = staff.ok("GET", "/auth/me")["user_id"]
    product = create_product(admin, price=800)
    oid = buy(user, product["product_id"])["order_id"]
    user.ok("POST", f"/orders/{oid}/pay")
    tid = ticket_service.create_ticket(user_id=uid, order_id=oid, content="申请退款")["id"]
    async def analyze(state):
        return {"decision": {"issue_type": "REFUND"}}
    async def generate(state):
        return {"result": {"solution": "办理退款"}, "business_action": "apply_refund", "need_human": True}
    monkeypatch.setattr(nodes.analyze, "analyze_ticket", analyze)
    monkeypatch.setattr(nodes.result, "generate_result", generate)
    real_action = nodes.action.execute_action
    real_resolve = ticket_service.resolve_ticket
    fired = False
    async def action(state):
        nonlocal fired
        result = await real_action(state)
        if fault == "after_resolution" and not fired:
            fired = True
            raise RuntimeError("crash before action checkpoint")
        return result
    def resolve(**kwargs):
        nonlocal fired
        if fault == "after_refund" and not fired:
            fired = True
            raise RuntimeError("crash after refund before resolution")
        return real_resolve(**kwargs)
    monkeypatch.setattr(nodes.action, "execute_action", action)
    monkeypatch.setattr(ticket_service, "resolve_ticket", resolve)
    graph = build_graph(InMemorySaver())
    monkeypatch.setattr(agent_service, "get_graph", lambda: graph)
    waiting = await agent_service.process_ticket(ticket_id=tid, user_id=uid)
    rid = waiting["agent_run"]["id"]
    real_finish = agent_run_repository.mark_finished
    def finish(*args, **kwargs):
        nonlocal fired
        if fault == "after_checkpoint" and kwargs.get("status") == "COMPLETED" and not fired:
            fired = True
            raise RuntimeError("crash after graph checkpoint before run settlement")
        return real_finish(*args, **kwargs)
    monkeypatch.setattr(agent_run_repository, "mark_finished", finish)
    with pytest.raises(RuntimeError, match="crash"):
        await agent_service.resume_with_decision(ticket_id=tid, operator_id=operator, approved=True)
    assert refund_count(oid, status="SUCCESS") == 1
    # 已批准并执行了的操作，不允许借“重试”改成驳回。
    with pytest.raises(AgentExecutionError, match="不能改成相反"):
        await agent_service.resume_with_decision(ticket_id=tid, operator_id=operator, approved=False)
    completed = await agent_service.resume_with_decision(ticket_id=tid, operator_id=operator, approved=True)
    assert completed["status"] == "COMPLETED"
    assert refund_count(oid, status="SUCCESS") == 1
    assert product_stock(product["product_id"]) == 10
    events = query_all("SELECT event_type FROM agent_run_events WHERE agent_run_id=%s", (rid,))
    for kind in ("WAITING_HUMAN", "HUMAN_APPROVED", "COMPLETED"):
        assert sum(row["event_type"] == kind for row in events) == 1
    messages = query_all("SELECT sender_type FROM ticket_messages WHERE ticket_id=%s", (tid,))
    assert sum(row["sender_type"] == "AGENT" for row in messages) == 1
    assert sum(row["sender_type"] == "SYSTEM" for row in messages) == 1
    assert query_one("SELECT agent_status FROM tickets WHERE id=%s", (tid,))["agent_status"] == "IDLE"


@pytest.mark.parametrize("table_name", ["ticket_messages", "agent_run_events"])
def test_legacy_tables_gain_nullable_key_and_unique_index(table_name, db_or_skip):
    import uuid
    from sqlalchemy import Column, MetaData, Table, create_engine, inspect, text
    from app.core.database import Base, engine as application_engine
    from app.core.schema import _sync_table
    # 本测试只修改自己创建的独立空库，不在业务表上删除列或索引。
    schema_name = "pytest_schema_" + uuid.uuid4().hex
    with application_engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4"))
    engine = create_engine(application_engine.url.set(database=schema_name))
    target = Base.metadata.tables[table_name]
    legacy = Table(table_name, MetaData(), *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in target.columns if c.name != "dedup_key"])
    try:
        legacy.create(engine)
        _sync_table(engine, inspect(engine), target)
        assert next(c for c in inspect(engine).get_columns(table_name) if c["name"] == "dedup_key")["nullable"]
        index = next(i for i in target.indexes if i.unique)
        assert any(i["name"] == index.name and i["unique"] for i in inspect(engine).get_indexes(table_name))
        index.drop(engine)
        _sync_table(engine, inspect(engine), target)
        assert any(i["name"] == index.name for i in inspect(engine).get_indexes(table_name))
    finally:
        engine.dispose()
        with application_engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE `{schema_name}`"))


@pytest.mark.asyncio
async def test_rejection_handoff_failure_can_resume(user, monkeypatch):
    uid, tid, _ = setup_ticket(user)
    async def analyze(state):
        return {"decision": {}}
    async def generate(state):
        return {"result": {"solution": "咨询处理"}, "need_human": True}
    monkeypatch.setattr(nodes.analyze, "analyze_ticket", analyze)
    monkeypatch.setattr(nodes.result, "generate_result", generate)
    graph = build_graph(InMemorySaver())
    monkeypatch.setattr(agent_service, "get_graph", lambda: graph)
    waiting = await agent_service.process_ticket(ticket_id=tid, user_id=uid)
    rid = waiting["agent_run"]["id"]
    original = ticket_service.add_message
    def fail_handoff(**kwargs):
        if kwargs.get("sender_type") == "SYSTEM":
            raise RuntimeError("handoff failed")
        return original(**kwargs)
    monkeypatch.setattr(ticket_service, "add_message", fail_handoff)
    with pytest.raises(RuntimeError, match="handoff failed"):
        await agent_service.resume_with_decision(ticket_id=tid, operator_id=uid,
                                                  approved=False, comment="原审批意见")
    assert ticket_service.get_active_run(tid)["status"] == "WAITING_HUMAN"
    monkeypatch.setattr(ticket_service, "add_message", original)
    result = await agent_service.resume_with_decision(ticket_id=tid, operator_id=uid,
                                                      approved=False, comment="重试的新意见")
    assert result["status"] == "REJECTED"
    messages = query_all("SELECT content FROM ticket_messages WHERE dedup_key=%s",
                         (f"run:{rid}:ticket:{tid}:rejection-handoff",))
    assert len(messages) == 1
    assert "原审批意见" in messages[0]["content"]


@pytest.mark.asyncio
async def test_second_real_review_is_not_deduplicated(user, monkeypatch):
    uid, tid, _ = setup_ticket(user)
    async def analyze(state):
        return {"decision": {}}
    async def generate(state):
        return {"result": {"solution": "咨询处理"}, "need_human": True}
    real_action = nodes.action.execute_action
    async def action(state):
        if state.get("human_review_round") == 1:
            return {"status": "WAITING_HUMAN", "need_human": True}
        return await real_action(state)
    monkeypatch.setattr(nodes.analyze, "analyze_ticket", analyze)
    monkeypatch.setattr(nodes.result, "generate_result", generate)
    monkeypatch.setattr(nodes.action, "execute_action", action)
    graph = build_graph(InMemorySaver())
    monkeypatch.setattr(agent_service, "get_graph", lambda: graph)
    waiting = await agent_service.process_ticket(ticket_id=tid, user_id=uid)
    rid = waiting["agent_run"]["id"]
    first = await agent_service.resume_with_decision(ticket_id=tid, operator_id=uid, approved=True)
    assert first["status"] == "WAITING_HUMAN"
    second = await agent_service.resume_with_decision(ticket_id=tid, operator_id=uid, approved=True)
    assert second["status"] == "COMPLETED"
    rows = query_all("SELECT event_type FROM agent_run_events WHERE agent_run_id=%s", (rid,))
    assert sum(r["event_type"] == "WAITING_HUMAN" for r in rows) == 2
    assert sum(r["event_type"] == "HUMAN_APPROVED" for r in rows) == 2
