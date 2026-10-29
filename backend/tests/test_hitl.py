"""HITL 人工接管：真中断、真恢复（v2.md §40 / §63）。

要验证的不是"有个 interrupt() 调用"，而是这几件事：

    · Agent 判定需要人工时：保存 Checkpoint、AgentRun=WAITING_HUMAN、工单=WAITING_HUMAN
    · 等待期间**不能**再起一次执行（重复点"处理"不新建 AgentRun）
    · 客服批准 → **恢复原 thread_id 的原图**（不重跑、不新建 run）→ 继续执行完
    · 客服驳回 → 结束 Agent 流程，转人工处理
    · 客服接管 → 终止 Agent + 工单归到自己名下（第三个出口）
    · 连点两次批准/接管只有一个生效（条件更新兜底）
    · 对**没有挂起**的工单批准/接管要返回业务错误，而不是 500

本文件里的 Agent 不会真的退款（桩的 business_action 为空）——
HITL 恢复后"真的把钱退了"在 test_e2e_chain.py 里验。
"""

from __future__ import annotations

import threading

import httpx
import pytest

from tests.conftest import (
    Api,
    Server,
    active_runs,
    concurrent,
    create_product,
    create_ticket,
    query_all,
    register_staff,
    run_event_messages,
    runs_of,
    stub_registry,
    ticket_row,
)

pytestmark = [pytest.mark.integration, pytest.mark.agent]


@pytest.fixture
def agent_user(agent_server: Server) -> Api:
    api = Api(agent_server.base_url)
    api.register_and_login("pytest_hitl")
    yield api
    api.close()


@pytest.fixture
def agent_staff(agent_server: Server) -> Api:
    api = Api(agent_server.base_url)
    api.login(username="service01", password="123456")
    yield api
    api.close()


def _ticket_waiting_human(agent_user: Api, agent_server: Server, *, content: str = "退款要人工") -> int:
    """造一张"Agent 已经判定需要人工"的工单，返回 ticket_id。"""
    ticket_id = create_ticket(agent_user, content=content)
    stub_registry(agent_server, "human", ticket_id)
    payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
    assert payload["status"] == "WAITING_HUMAN", payload
    return ticket_id


# ======================================================================
# 进入等待人工：状态与 Checkpoint
# ======================================================================
class TestEnterWaitingHuman:
    def test_stops_and_marks_waiting_human(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)

        run = runs_of(ticket_id)[0]
        assert run["status"] == "WAITING_HUMAN"
        assert run["need_human"] in (1, True)

        row = ticket_row(ticket_id)
        assert row["status"] == "WAITING_HUMAN", "工单没有被置为等待人工"
        assert row["agent_status"] == "WAITING_HUMAN", "执行权没有停在等待人工"
        assert row["agent_run_id"] == run["id"], "执行权没有绑定到这条 run"

    def test_checkpoint_is_saved_with_thread_id(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """thread_id 必须落库 —— 它是恢复图的唯一钥匙（没有它就无法 resume）。"""
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        run = runs_of(ticket_id)[0]
        assert run["thread_id"], "AgentRun 没有 thread_id，无法恢复"

    def test_waiting_event_is_emitted(self, agent_server: Server, agent_user: Api) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        run_id = runs_of(ticket_id)[0]["id"]
        events = [
            row["event_type"]
            for row in query_all(
                "SELECT event_type FROM agent_run_events WHERE agent_run_id=%s ORDER BY id",
                (run_id,),
            )
        ]
        assert "WAITING_HUMAN" in events, f"缺少 WAITING_HUMAN 事件：{events}"

    def test_processing_again_while_waiting_creates_no_new_run(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """等待人工期间再点"处理"：不新建 AgentRun，如实告诉调用方在等人工。"""
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        before = runs_of(ticket_id)

        payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "WAITING_HUMAN", payload
        assert payload["used_existing_run"] is True

        assert runs_of(ticket_id) == before, "等待人工期间竟然新建了 AgentRun"

    def test_concurrent_process_while_waiting_creates_no_new_run(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        before = len(runs_of(ticket_id))

        concurrent(
            "POST", agent_server.base_url, f"/tickets/{ticket_id}/process", agent_user.token, 4
        )
        assert len(runs_of(ticket_id)) == before, "并发点处理在等待人工期间新建了 run"


# ======================================================================
# 批准：恢复原图
# ======================================================================
class TestApprove:
    def test_approve_resumes_same_run(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """批准 → 恢复的是**同一条 AgentRun**（不是新起一次），并推进到 COMPLETED。"""
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        original = runs_of(ticket_id)[0]

        agent_staff.ok("POST", f"/tickets/{ticket_id}/approve", json={"comment": "同意退款"})

        runs = runs_of(ticket_id)
        assert len(runs) == 1, f"批准后新建了 run：{runs}"
        assert runs[0]["id"] == original["id"], "恢复的不是原来那条 run"
        assert runs[0]["thread_id"] == original["thread_id"], "thread_id 变了，说明重跑了"
        assert runs[0]["status"] == "COMPLETED"

        row = ticket_row(ticket_id)
        assert row["agent_status"] == "IDLE", "批准执行完后执行权没有释放"

    def test_approve_records_human_decision(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """人工审批要留痕：HUMAN_APPROVED 事件 + 审批意见。"""
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        agent_staff.ok("POST", f"/tickets/{ticket_id}/approve", json={"comment": "客服已核对"})

        run_id = runs_of(ticket_id)[0]["id"]
        types = [
            row["event_type"]
            for row in query_all(
                "SELECT event_type FROM agent_run_events WHERE agent_run_id=%s ORDER BY id",
                (run_id,),
            )
        ]
        assert "HUMAN_APPROVED" in types, types
        messages = run_event_messages([run_id], "客服已核对")
        assert messages, "审批意见没有进事件流"

    def test_double_approve_only_resumes_once(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """客服连点两次"批准"：只有一次能真的恢复图（条件更新抢执行权）。"""
        ticket_id = _ticket_waiting_human(agent_user, agent_server)

        responses = concurrent(
            "POST",
            agent_server.base_url,
            f"/tickets/{ticket_id}/approve",
            agent_staff.token,
            2,
            json_body={"comment": "连点了两次"},
        )
        codes = sorted(resp.status_code for resp in responses)
        assert codes.count(200) == 1, f"两次批准应当只有一次生效：{codes}"

        runs = runs_of(ticket_id)
        assert len(runs) == 1, f"重复批准产生了新的 run：{runs}"
        assert runs[0]["status"] == "COMPLETED"

        # 只应当有一条"批准"事件（第二次请求根本没恢复图）
        approved = run_event_messages([runs[0]["id"]], "客服已批准")
        assert len(approved) == 1, f"批准事件出现了 {len(approved)} 次"

    def test_approve_without_waiting_run_is_business_error(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """对一张从没跑过 Agent 的工单批准：要 409 业务错误，不是 500。"""
        ticket_id = create_ticket(agent_user, content="还没跑过 Agent")
        resp = agent_staff.call("POST", f"/tickets/{ticket_id}/approve")
        assert resp.status_code in (400, 409), resp.text[:200]
        assert resp.json()["code"] in ("AGENT_EXECUTION_FAILED", "AGENT_WAITING_HUMAN", "INVALID_STATE")


# ======================================================================
# 驳回
# ======================================================================
class TestReject:
    def test_reject_ends_agent_flow(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        agent_staff.ok("POST", f"/tickets/{ticket_id}/reject", json={"reason": "不符合退款条件"})

        run = runs_of(ticket_id)[0]
        assert run["status"] == "REJECTED"

        row = ticket_row(ticket_id)
        assert row["agent_status"] == "IDLE", "驳回后执行权没有释放"
        assert row["status"] == "PROCESSING", "驳回后工单应当回到处理中（交人工）"

        types = [
            r["event_type"]
            for r in query_all(
                "SELECT event_type FROM agent_run_events WHERE agent_run_id=%s ORDER BY id",
                (run["id"],),
            )
        ]
        assert "HUMAN_REJECTED" in types, types

    def test_reject_does_not_execute_refund(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """驳回绝不能顺手把钱退了。"""
        from tests.conftest import refund_count

        from tests.conftest import buy, create_product

        admin = Api(agent_server.base_url)
        admin.login(username="admin", password="admin123")
        try:
            product = create_product(admin, price=60.0, stock=3)
        finally:
            admin.close()

        order = buy(agent_user, product["product_id"])
        agent_user.ok("POST", f"/orders/{order['order_id']}/pay")

        ticket_id = create_ticket(
            agent_user, content="退款请求", order_id=order["order_id"], type="REFUND"
        )
        stub_registry(agent_server, "human", ticket_id)
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        agent_staff.ok("POST", f"/tickets/{ticket_id}/reject", json={"reason": "拒绝"})
        assert refund_count(order["order_id"]) == 0, "驳回竟然执行了退款"


# ======================================================================
# 接管（HITL 的第三个出口）
# ======================================================================
class TestTakeover:
    def test_takeover_terminates_agent_and_assigns_ticket(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)

        detail = agent_staff.ok("POST", f"/tickets/{ticket_id}/takeover", json={"comment": "我来处理"})

        run = runs_of(ticket_id)[0]
        assert run["status"] == "REJECTED", "接管应当终止 Agent 执行"

        row = ticket_row(ticket_id)
        assert row["agent_status"] == "IDLE", "接管后执行权没有释放"
        assert row["status"] == "PROCESSING", "接管后工单应当回到处理中"

        staff_id = agent_staff.ok("GET", "/auth/me")["user_id"]
        assert detail["assignee_id"] == staff_id, "接管后工单没有归到客服名下"

    def test_takeover_writes_a_system_message(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        agent_staff.ok("POST", f"/tickets/{ticket_id}/takeover", json={"comment": "我来跟进"})

        detail = agent_user.ok("GET", f"/tickets/{ticket_id}")
        system = [m for m in detail["messages"] if m["sender_type"] == "SYSTEM"]
        assert system, "接管没有留下任何系统消息"
        # 应当既说明"接管"这个动作，也带上客服的备注
        assert any("接管" in m["content"] for m in system), system
        assert any("我来跟进" in m["content"] for m in system), system

    def test_double_takeover_only_applies_once(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """连点两次"接管"：只有一次真的接管，第二次是幂等返回。

        "只生效一次"体现在**副作用**上，而不只是状态码：
        系统消息只能出现一条、Agent 执行只能被终止一次。
        （竞态窗口里的第二次请求会撞条件更新拿 409；
        第一次已经落定之后的第二次请求则应当幂等返回而不是报错 ——
        客服重复点一下不该看到红色错误。）
        """
        ticket_id = _ticket_waiting_human(agent_user, agent_server)

        first = agent_staff.ok("POST", f"/tickets/{ticket_id}/takeover", json={"comment": "我来处理"})
        second = agent_staff.ok("POST", f"/tickets/{ticket_id}/takeover", json={"comment": "再来一次"})

        # 两次都返回"接管后的状态"，且状态完全一致
        assert first["assignee_id"] == second["assignee_id"]
        assert first["status"] == second["status"] == "PROCESSING"

        # 关键：副作用只发生了一次
        system = [m for m in second["messages"] if m["sender_type"] == "SYSTEM"]
        takeover_messages = [m for m in system if "接管" in m["content"]]
        assert len(takeover_messages) == 1, f"接管写了 {len(takeover_messages)} 条消息：{takeover_messages}"

        # Agent 执行只被终止一次（只有一条 run，且是 REJECTED）
        runs = runs_of(ticket_id)
        assert len(runs) == 1
        assert runs[0]["status"] == "REJECTED"

    def test_concurrent_takeover_only_one_wins(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """真并发（Barrier 对齐）：**两个不同的客服**同时抢同一张等待人工的工单。

        必须是两个不同的操作者：同一个 token 发两次请求在业务上就是"同一个人连点两次"，
        本来就该幂等成功（那是正确行为，不是竞态）。

        判据是归属的条件更新（`WHERE assignee_id IS NULL`）：
        赢的那个 200、输的那个 409 TAKEOVER_CONFLICT。
        """
        staff_a = register_staff(agent_server, "pytest_cs_a")
        staff_b = register_staff(agent_server, "pytest_cs_b")
        try:
            ticket_id = _ticket_waiting_human(agent_user, agent_server)

            barrier = threading.Barrier(2)
            responses: list = [None, None]

            def grab(index: int, api: Api) -> None:
                with httpx.Client(
                    base_url=f"{agent_server.base_url}/api", timeout=60.0, trust_env=False
                ) as raw:
                    barrier.wait()
                    responses[index] = raw.post(
                        f"/tickets/{ticket_id}/takeover",
                        headers=api.headers(),
                        json={"comment": f"客服{index}接管"},
                    )

            threads = [
                threading.Thread(target=grab, args=(0, staff_a)),
                threading.Thread(target=grab, args=(1, staff_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            codes = sorted(resp.status_code for resp in responses)
            assert codes.count(200) == 1, f"两个客服抢单应当只有一个成功：{codes}"
            assert codes.count(409) == 1, f"另一个应当返回 409：{codes}"

            rejected = [r.json() for r in responses if r.status_code == 409][0]
            assert rejected["code"] == "TAKEOVER_CONFLICT"

            # 归属只能是赢的那个，且只剩一条接管消息
            winner_id = [r.json()["assignee_id"] for r in responses if r.status_code == 200][0]
            assert winner_id in (
                staff_a.ok("GET", "/auth/me")["user_id"],
                staff_b.ok("GET", "/auth/me")["user_id"],
            )
            assert ticket_row(ticket_id)["assignee_id"] == winner_id

            detail = agent_user.ok("GET", f"/tickets/{ticket_id}")
            takeover_messages = [
                m
                for m in detail["messages"]
                if m["sender_type"] == "SYSTEM" and "接管" in m["content"]
            ]
            assert len(takeover_messages) == 1, f"接管消息应当只有一条：{takeover_messages}"
        finally:
            staff_a.close()
            staff_b.close()

    def test_takeover_on_idle_ticket_is_allowed(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """没有 Agent 执行的工单也能接管（纯归属接管）—— 客服只是"领单"。"""
        ticket_id = create_ticket(agent_user, content="普通工单，不需要 Agent")

        detail = agent_staff.ok("POST", f"/tickets/{ticket_id}/takeover")
        staff_id = agent_staff.ok("GET", "/auth/me")["user_id"]
        assert detail["assignee_id"] == staff_id
        assert ticket_row(ticket_id)["agent_status"] == "IDLE"


# ======================================================================
# 状态不变量
# ======================================================================
class TestHitlStateInvariants:
    def test_waiting_ticket_has_exactly_one_active_run(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        assert len(active_runs(ticket_id)) == 1

    def test_approve_then_process_starts_a_fresh_run(
        self, agent_server: Server, agent_user: Api, agent_staff: Api
    ) -> None:
        """批准跑完之后，用户再点"处理"是**新的一次执行**（历史保留）。"""
        ticket_id = _ticket_waiting_human(agent_user, agent_server)
        agent_staff.ok("POST", f"/tickets/{ticket_id}/approve")

        # 桩里这张工单还在 human 名单里，所以再跑一次依旧会挂起 —— 但必须是新 run
        payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["used_existing_run"] is False

        runs = runs_of(ticket_id)
        assert len(runs) == 2, f"应当有两条 run（历史保留）：{runs}"
        assert runs[0]["thread_id"] != runs[1]["thread_id"], "两次执行共用了 thread_id"
