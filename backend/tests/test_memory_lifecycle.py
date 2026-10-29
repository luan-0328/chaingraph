"""历史摘要写入、隔离、失效、并发去重及生成节点读取。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.database import session_scope
from app.memory import user_memory
from app.repositories import user_memory_repository
from app.services import ticket_service
from tests.conftest import query_one


def make_ticket(user):
    uid = user.ok("GET", "/auth/me")["user_id"]
    ticket = ticket_service.create_ticket(user_id=uid, content="包裹问题")
    return uid, ticket["id"]


def test_summary_lifecycle_and_user_isolation(user, other_user):
    uid, tid = make_ticket(user)
    other_uid = other_user.ok("GET", "/auth/me")["user_id"]
    assert user_memory.recall(uid) == []
    ticket_service.resolve_ticket(ticket_id=tid, solution="请查看订单物流")
    memory = user_memory.recall(uid)
    assert len(memory) == 1 and memory[0]["source"] == f"ticket:{tid}"
    assert "请查看订单物流" in memory[0]["content"]
    assert user_memory.recall(other_uid) == []
    assert user_memory.recall(uid, exclude_ticket_id=tid) == []
    ticket_service.reopen_ticket(ticket_id=tid, user_id=uid)
    assert user_memory.recall(uid) == []
    ticket_service.resolve_ticket(ticket_id=tid, solution="客服重新确认后的结论")
    memory = user_memory.recall(uid)
    assert len(memory) == 1 and "重新确认" in memory[0]["content"]
    ticket_service.close_ticket(ticket_id=tid, user_id=uid)
    assert len(user_memory.recall(uid)) == 1


def test_concurrent_resolution_writes_one_summary(user):
    uid, tid = make_ticket(user)
    barrier = threading.Barrier(4)
    def resolve(i):
        barrier.wait(timeout=5)
        return ticket_service.resolve_ticket(ticket_id=tid, solution=f"结论{i}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(resolve, range(4)))
    assert query_one("SELECT COUNT(*) AS n FROM user_memories WHERE user_id=%s AND source=%s",
                     (uid, f"ticket:{tid}"))["n"] == 1


def test_memory_write_and_resolution_are_atomic(user, monkeypatch):
    uid, tid = make_ticket(user)
    monkeypatch.setattr(user_memory_repository, "save_ticket_summary", MagicMock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError):
        ticket_service.resolve_ticket(ticket_id=tid, solution="还未提交")
    assert ticket_service.get_ticket_raw(tid)["status"] == "OPEN"
    assert user_memory.recall(uid) == []


def test_memory_api_owner_can_read_delete_but_other_user_cannot(user, other_user):
    uid, tid = make_ticket(user)
    ticket_service.resolve_ticket(ticket_id=tid, solution="处理完成")
    memories = user.ok("GET", "/auth/me/memories")
    assert len(memories) == 1
    assert other_user.ok("GET", "/auth/me/memories") == []
    path = f"/auth/me/memories/{memories[0]['id']}"
    assert other_user.status("DELETE", path) == 403
    assert user.status("DELETE", path) == 204
    assert user_memory.recall(uid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_generate_reads_memory_even_without_business_query(monkeypatch, unavailable):
    from app.agent.nodes import result
    from app.agent.schemas import AgentResult
    tool = MagicMock()
    if unavailable:
        tool.invoke.side_effect = RuntimeError("db temporarily unavailable")
    else:
        tool.invoke.return_value = {"success": True, "data": [{"content": "旧工单处理摘要", "source": "ticket:9"}]}
    monkeypatch.setitem(result.QUERY_TOOLS, "query_user_memory", tool)
    monkeypatch.setattr(result, "emit_event", MagicMock())
    llm = AsyncMock(return_value=AgentResult(issue_type="OTHER", summary="咨询", analysis="分析", solution="请提供订单信息"))
    monkeypatch.setattr(result, "ainvoke_structured", llm)
    state = {"user_id": 7, "ticket_id": 10, "content": "咨询"}
    first = await result.generate_result(state)
    second = await result.generate_result({**state, **first})
    tool.invoke.assert_called_with({"user_id": 7, "limit": 3, "exclude_ticket_id": 10})
    assert len(second["tool_results"]) == 1
    assert second["used_tool"]
    prompt = llm.call_args.args[1][1]["content"]
    assert ("MEMORY_UNAVAILABLE" if unavailable else "旧工单处理摘要") in prompt


def test_refund_summary_filters_user_before_pagination(db_or_skip, monkeypatch):
    from app.repositories import refund_repository
    query = MagicMock(return_value=([{"status": "SUCCESS"}], 61))
    monkeypatch.setattr(refund_repository, "list_all", query)
    assert "61" in user_memory.summarize_refund_history(7)
    assert query.call_args.kwargs == {"limit": 1, "user_id": 7}
