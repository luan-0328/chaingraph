"""Agent 异常恢复与可观测性（v2.md §42 / §43 / §44 / §63）。

被测的是"Agent 出问题时系统还能不能自洽"：

    · 可恢复异常（超时/429）→ 自动重试，**断点续跑**，已跑完的只读节点不重跑
    · 不可恢复异常（业务/鉴权）→ 立刻失败，不浪费重试
    · 失败也必须释放执行权（工单不能永久卡在 RUNNING）
    · 失败原因如实落库（AgentRun.error + 事件流），用户重新发起是新的一条 run
    · 僵尸执行（进程被杀留下的 RUNNING）能被回收
    · **同一工单并发处理只创建一条 AgentRun**（互斥的核心指标）
    · 事件级可观测性：STARTED → ANALYZING → RETRIEVING_KNOWLEDGE → … → COMPLETED
    · current_node 反映"此刻卡在哪个节点"，跑完清空

全部走确定性 LLM 桩（`:8022`，`business_action` 为空）——
所以这些测试**不会真的退款**，只测调度与记录；"真的退钱"在 test_e2e_chain.py。
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    Api,
    Server,
    active_runs,
    buy,
    concurrent,
    create_after_sale,
    create_product,
    create_ticket,
    execute,
    fail_node_once,
    query_all,
    restore_nodes,
    run_event_messages,
    runs_of,
    stub_registry,
    ticket_row,
)

pytestmark = [pytest.mark.integration, pytest.mark.agent]


@pytest.fixture
def agent_user(agent_server: Server) -> Api:
    api = Api(agent_server.base_url)
    api.register_and_login("pytest_agent")
    yield api
    api.close()


@pytest.fixture
def agent_admin(agent_server: Server) -> Api:
    api = Api(agent_server.base_url)
    api.login(username="admin", password="admin123")
    yield api
    api.close()


@pytest.fixture(autouse=True)
def _clean_injected_nodes(agent_server: Server):
    """每个测试后恢复被注入故障的节点 —— 否则会污染同实例上的后续测试。"""
    yield
    restore_nodes(agent_server)


# ======================================================================
# 失败与重试
# ======================================================================
class TestRetryableFailure:
    def test_retryable_error_is_retried_then_succeeds(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """第一次模型调用抛超时 → 自动重试 → 整体成功，且**仍是同一条 AgentRun**。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        stub_registry(agent_server, "fail", ticket_id)

        payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "COMPLETED", payload

        runs = runs_of(ticket_id)
        assert len(runs) == 1, f"重试不该新建 AgentRun：{runs}"
        assert runs[0]["status"] == "COMPLETED"
        assert runs[0]["retry_count"] >= 1, "没有记录重试次数"

    def test_failure_reason_is_recorded_in_event_stream(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """重试成功后，**上一次的失败原因仍然留在事件流里**（历史可追溯）。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        stub_registry(agent_server, "fail", ticket_id)
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        run_ids = [run["id"] for run in runs_of(ticket_id)]
        failures = run_event_messages(run_ids, "失败")
        assert failures, "事件流里没有留下失败原因"

    def test_retry_does_not_rerun_completed_nodes(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """断点续跑的意义：已经跑完的 analyze 不该再跑一遍。

        「正在分析工单」这条事件出现两次 = 从头重跑了（那就白费了前面的检索/查询）。
        """
        ticket_id = create_ticket(agent_user, content="我要退款")
        fail_node_once(agent_server, "retrieve_knowledge", ticket_id)

        payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "COMPLETED", payload

        runs = runs_of(ticket_id)
        assert len(runs) == 1
        assert runs[0]["retry_count"] >= 1
        assert runs[0]["resumed"] in (1, True), "没有走断点续跑（resumed 未置位）"

        analyze_events = run_event_messages([runs[0]["id"]], "正在分析工单")
        assert len(analyze_events) == 1, (
            f"analyze 节点跑了 {len(analyze_events)} 次，说明是整图重跑而不是断点续跑"
        )

    def test_resume_clears_last_error_after_success(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """续跑成功后清空 error 字段（失败详情留在事件流里，不留在当前状态里）。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        fail_node_once(agent_server, "retrieve_knowledge", ticket_id)
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        run = runs_of(ticket_id)[0]
        assert run["status"] == "COMPLETED"
        assert run["error"] is None, f"续跑成功却还留着失败原因：{run['error']}"


class TestFatalFailure:
    def test_fatal_error_is_not_retried(self, agent_server: Server, agent_user: Api) -> None:
        """不可恢复异常：一次就失败，绝不反复重试。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        stub_registry(agent_server, "fatal", ticket_id)

        resp = agent_user.call("POST", f"/tickets/{ticket_id}/process")
        assert resp.status_code >= 400, "不可恢复异常应当失败"

        runs = runs_of(ticket_id)
        assert len(runs) == 1
        assert runs[0]["status"] == "FAILED"
        assert runs[0]["retry_count"] == 0, "不可恢复异常不该重试"

    def test_failure_records_reason_and_releases_execution(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """失败原因要落库，执行权要释放 —— 否则工单永久卡死，用户再也点不动。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        stub_registry(agent_server, "fatal", ticket_id)
        agent_user.call("POST", f"/tickets/{ticket_id}/process")

        run = runs_of(ticket_id)[0]
        assert run["error"], "失败原因没有落库"
        assert "鉴权" in run["error"] or "invalid api key" in run["error"].lower()

        row = ticket_row(ticket_id)
        assert row["agent_status"] == "IDLE", "失败后执行权没有释放"
        assert row["status"] != "PROCESSING", "工单卡在 PROCESSING 了"

    def test_user_can_restart_after_failure(self, agent_server: Server, agent_user: Api) -> None:
        """失败后用户重新发起 = **新的一条 AgentRun**（历史保留，不覆盖）。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        stub_registry(agent_server, "fatal", ticket_id)

        agent_user.call("POST", f"/tickets/{ticket_id}/process")
        assert runs_of(ticket_id)[0]["status"] == "FAILED"

        # 桩只让"第一次调用"失败，所以重新发起会成功
        payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "COMPLETED"

        runs = runs_of(ticket_id)
        assert len(runs) == 2, f"重新发起应当新增一条 run（历史保留）：{runs}"
        assert runs[0]["status"] == "FAILED"
        assert runs[1]["status"] == "COMPLETED"


# ======================================================================
# 并发互斥（同工单只跑一次）
# ======================================================================
class TestSameTicketMutex:
    def test_concurrent_process_creates_single_run(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """同一工单 6 个并发 /process：数据库里只能有一条 AgentRun。"""
        ticket_id = create_ticket(agent_user, content="我要退款")

        responses = concurrent(
            "POST", agent_server.base_url, f"/tickets/{ticket_id}/process", agent_user.token, 6
        )
        payloads = [resp.json() for resp in responses if resp.status_code == 200]
        assert payloads, "至少要有一个请求成功"

        assert len(runs_of(ticket_id)) == 1, (
            f"并发处理创建了多条 AgentRun：{runs_of(ticket_id)}"
        )

        # 除了真的执行的那一次，其余都应当明确告诉调用方"复用了已有执行"
        reused = [p for p in payloads if p.get("used_existing_run") or p["status"] == "ALREADY_RUNNING"]
        assert reused, "没有请求返回 ALREADY_RUNNING / used_existing_run"

    def test_only_one_request_actually_executes(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """恰好一个请求真的跑了 Agent（其余复用），且活跃执行只有一个。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        responses = concurrent(
            "POST", agent_server.base_url, f"/tickets/{ticket_id}/process", agent_user.token, 6
        )

        executed = [
            resp.json()
            for resp in responses
            if resp.status_code == 200 and not resp.json().get("used_existing_run")
        ]
        assert len(executed) == 1, f"真的执行了 {len(executed)} 次：{executed}"

        row = ticket_row(ticket_id)
        assert row["agent_status"] == "IDLE", "执行结束后执行权应当回到 IDLE"
        assert len(active_runs(ticket_id)) == 0

    def test_different_tickets_run_concurrently(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """不同工单之间**不能**有全局锁 —— 否则"并发"是假的。

        桩把每次模型调用固定 sleep 0.8s，一次执行 ≈ 1.6s。
        3 张工单串行需要 ~4.8s；真的并发应当明显更快。
        """
        import threading
        import time

        ticket_ids = [create_ticket(agent_user, content=f"我要退款 {i}") for i in range(3)]
        barrier = threading.Barrier(len(ticket_ids))
        durations: list[float] = []

        def run(ticket_id: int) -> None:
            barrier.wait()
            started = time.time()
            agent_user.ok("POST", f"/tickets/{ticket_id}/process")
            durations.append(time.time() - started)

        threads = [threading.Thread(target=run, args=(tid,)) for tid in ticket_ids]
        start = time.time()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.time() - start

        assert len(runs_of(ticket_ids[0])) == 1
        assert wall < sum(durations) * 0.75, (
            f"3 张工单总耗时 {wall:.2f}s，接近串行总和 {sum(durations):.2f}s —— 存在全局锁"
        )


# ======================================================================
# 僵尸执行回收
# ======================================================================
class TestZombieExecution:
    def test_new_unbound_execution_is_not_reaped(self, agent_user: Api) -> None:
        from app.services import ticket_service

        ticket_id = create_ticket(agent_user, content="新执行抢占窗口")
        assert ticket_service.acquire_agent_execution(ticket_id)
        try:
            assert ticket_service.reap_stale_execution(
                ticket_id, stale_seconds=1800, still_expected_run_id=None
            ) is None
            assert ticket_row(ticket_id)["agent_status"] == "RUNNING"
        finally:
            ticket_service.settle_agent_execution(ticket_id, agent_run_id=None)

    def test_old_execution_cannot_release_or_rebind_new_run(self, agent_user: Api) -> None:
        from app.core.database import session_scope
        from app.repositories import ticket_repository
        from app.services import agent_service

        ticket_id = create_ticket(agent_user, content="执行归属校验")
        execute("UPDATE tickets SET agent_status='RUNNING', agent_run_id=999 WHERE id=%s", (ticket_id,))
        agent_service._settle_after_execution(ticket_id, agent_run_id=7, final_status="COMPLETED")
        assert ticket_row(ticket_id)["agent_status"] == "RUNNING"
        with session_scope() as db:
            assert ticket_repository.bind_agent_execution(db, ticket_id, agent_run_id=7) == 0
        assert ticket_row(ticket_id)["agent_run_id"] == 999

    def test_resume_requires_matching_run(self, agent_user: Api) -> None:
        from app.services import ticket_service

        ticket_id = create_ticket(agent_user, content="审批归属校验")
        execute("UPDATE tickets SET agent_status='WAITING_HUMAN', agent_run_id=999 WHERE id=%s", (ticket_id,))
        assert not ticket_service.acquire_agent_execution_for_resume(ticket_id, agent_run_id=7)
        row = ticket_row(ticket_id)
        assert row["agent_status"] == "WAITING_HUMAN"
        assert row["agent_run_id"] == 999

    def test_stale_running_is_reaped(self, agent_server: Server, agent_user: Api) -> None:
        """模拟"进程被杀"：工单卡在 RUNNING、心跳停在两小时前 → 后续请求能接管。

        注意这里用 `UTC_TIMESTAMP()` 而不是 `NOW()`：
        应用统一写 UTC（见 core/time.py），而 MySQL 的 `NOW()` 是**服务器本地时间**。
        东八区下用 NOW() 写出来的"两小时前"其实比当前 UTC 还晚 6 小时，
        算出来的心跳年龄是负数 → 被当成"最新鲜的心跳"，僵尸永远收不掉。
        （ticket_service._is_run_alive 对这种情况有专门的告警分支，
        但真实场景里"手工改库写错时区"同样会造成卡死，所以测试必须写对。）
        """
        ticket_id = create_ticket(agent_user, content="我要退款")
        # 人为造一个僵尸：一条 RUNNING 的 run + 工单执行权指向它
        execute(
            "INSERT INTO agent_runs "
            "(ticket_id, user_id, thread_id, status, resumed, started_at, alive_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'RUNNING', 0, UTC_TIMESTAMP(), "
            "        DATE_SUB(UTC_TIMESTAMP(), INTERVAL 2 HOUR), UTC_TIMESTAMP(), UTC_TIMESTAMP())",
            (ticket_id, agent_user.ok("GET", "/auth/me")["user_id"], f"zombie-{ticket_id}"),
        )
        zombie = runs_of(ticket_id)[-1]
        execute(
            "UPDATE tickets SET agent_status='RUNNING', agent_run_id=%s WHERE id=%s",
            (zombie["id"], ticket_id),
        )
        assert ticket_row(ticket_id)["agent_status"] == "RUNNING"

        # 现在再点"处理"：应当识别出僵尸、回收执行权，然后正常跑完
        payload = agent_user.ok("POST", f"/tickets/{ticket_id}/process")
        assert payload["status"] == "COMPLETED", payload

        assert ticket_row(ticket_id)["agent_status"] == "IDLE"
        reaped = [run for run in runs_of(ticket_id) if run["id"] == zombie["id"]][0]
        assert reaped["status"] == "FAILED", "僵尸 run 没有被标记为失败"
        assert reaped["error"], "僵尸 run 没有写明回收原因"


# ======================================================================
# 可观测性：事件 + current_node
# ======================================================================
class TestObservability:
    def test_run_emits_full_business_event_sequence(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """一次完整执行要落下 STARTED → ANALYZING → RETRIEVING… → COMPLETED。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        run_id = runs_of(ticket_id)[0]["id"]
        event_types = [
            row["event_type"]
            for row in query_all(
                "SELECT event_type FROM agent_run_events WHERE agent_run_id=%s ORDER BY id",
                (run_id,),
            )
        ]

        assert event_types[0] == "STARTED", event_types
        assert "COMPLETED" in event_types, event_types
        # 顺序：先分析，再检索/调工具，最后才是完成
        assert event_types.index("ANALYZING") < event_types.index("COMPLETED")
        assert any(
            name in event_types
            for name in ("RETRIEVING_KNOWLEDGE", "QUERY_ORDER", "TOOL_CALLING")
        ), f"事件流里没有任何检索/查询步骤：{event_types}"

    def test_events_are_persisted_not_only_pushed(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """SSE 只是通知：断线/刷新之后，事件仍然能从数据库读出来。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        detail = agent_user.ok("GET", f"/tickets/{ticket_id}")
        assert detail["agent_events"], "工单详情里读不到 Agent 事件"
        assert all("event_type" in event and "message" in event for event in detail["agent_events"])

    def test_current_node_is_cleared_after_finish(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """跑完之后 current_node 必须清空 —— 否则客服会以为它还在跑。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        run = runs_of(ticket_id)[0]
        assert run["status"] == "COMPLETED"
        assert run["current_node"] is None, f"跑完了 current_node 还留着 {run['current_node']}"

    def test_run_records_rag_and_tool_usage(
        self, agent_server: Server, agent_user: Api
    ) -> None:
        """used_rag / used_tool 如实反映这次执行用了什么（工作台据此复盘）。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        run = runs_of(ticket_id)[0]
        assert run["used_rag"] in (1, True), "桩的决策要求检索，used_rag 应当为真"

    def test_retry_event_is_recorded(self, agent_server: Server, agent_user: Api) -> None:
        """重试本身也要是个可见事件（客服能看到"刚才抖了一下，已重试"）。"""
        ticket_id = create_ticket(agent_user, content="我要退款")
        stub_registry(agent_server, "fail", ticket_id)
        agent_user.ok("POST", f"/tickets/{ticket_id}/process")

        run_id = runs_of(ticket_id)[0]["id"]
        retries = run_event_messages([run_id], "重试")
        assert retries, "事件流里没有重试记录"
