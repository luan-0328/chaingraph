"""仅供隔离测试使用的崩溃注入入口，不向业务 API 暴露控制端点。"""

import asyncio
import os
import re
import time
from pathlib import Path

from sqlalchemy.engine import make_url


def main():
    """在真实提交窗口直接退出进程，验证重新启动后的恢复与资金幂等性。"""
    name = os.environ.get("ASYNC_TEST_DATABASE", "")
    if (not re.fullmatch(r"agent_async_test_[0-9a-f]{32}", name)
            or make_url(os.environ["DATABASE_URL"]).database != name):
        raise RuntimeError("故障 worker 只能由隔离回归启动器使用")
    from app import worker
    from app.agent import nodes
    from app.services.side_effect_service import SideEffectGuard, SideEffectKind

    fault = os.environ["ASYNC_TEST_FAULT"]
    if fault == "slow_upload_cleanup":
        from app.services import attachment_service
        folder = Path(os.environ["ASYNC_TEST_FOLDER"])

        def slow_cleanup():
            (folder / "oss-cleanup-started").touch()
            deadline = time.monotonic() + 20
            while not (folder / "oss-cleanup-release").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            return 0

        attachment_service.cleanup_uploads = slow_cleanup
    elif fault == "pause_before_refund":
        original = nodes.action._refund_with_idempotency_guard

        def pause_before_refund(**kwargs):
            """节点已经开始执行，但业务事务尚未开始时等待用户交接请求。"""
            (Path(os.environ["ASYNC_TEST_FOLDER"]) / "handoff-ready").touch()
            time.sleep(1.5)
            return original(**kwargs)

        nodes.action._refund_with_idempotency_guard = pause_before_refund
    elif fault == "pause_after_refund":
        original = SideEffectGuard.complete

        def pause_after_refund(self, result):
            """真实退款已提交，暂停在结果快照之前，不能把后来的交接当作撤销。"""
            if self.kind == SideEffectKind.REFUND and result and result.get("executed"):
                (Path(os.environ["ASYNC_TEST_FOLDER"]) / "handoff-ready").touch()
                time.sleep(1.5)
            return original(self, result)

        SideEffectGuard.complete = pause_after_refund
    elif fault in ("human_plan", "semantic_handoff"):
        from app.agent.stub_llm import _StubStructuredLlm
        if fault == "human_plan":
            original = _StubStructuredLlm._result

            def human_plan(self, ticket_id):
                """保留真实生成节点，只让模型桩输出无法执行的人工方案。"""
                return original(self, ticket_id).model_copy(update={"resolution_kind": "HUMAN", "business_action": ""})

            _StubStructuredLlm._result = human_plan
        else:
            original = _StubStructuredLlm._decision

            def semantic_handoff(self, ticket_id):
                """模拟模型对复杂真人诉求的结构化识别，不通过正则捷径。"""
                return original(self, ticket_id).model_copy(update={"request_human": True})

            _StubStructuredLlm._decision = semantic_handoff
    elif fault == "after_refund_commit":
        complete = SideEffectGuard.complete

        def crash_before_snapshot(self, result):
            """退款事务已提交，但幂等结果快照尚未写入。"""
            if self.kind == SideEffectKind.REFUND and result and result.get("executed"):
                os._exit(86)
            return complete(self, result)

        SideEffectGuard.complete = crash_before_snapshot
    elif fault == "after_action":
        action = nodes.action.execute_action

        async def crash_after_action(state):
            """退款与工单回复均已提交，图尚未保存节点完成状态。"""
            result = await action(state)
            if result.get("status") == "COMPLETED":
                os._exit(86)
            return result

        nodes.action.execute_action = crash_after_action
    elif fault == "before_finish":
        finish = worker.tasks.finish_agent_run

        def crash_before_finish(run_id, state):
            """图已完成并保存 Checkpoint，MySQL 任务状态尚未收尾。"""
            if state.get("status") == "COMPLETED":
                os._exit(86)
            return finish(run_id, state)

        worker.tasks.finish_agent_run = crash_before_finish
    else:
        raise ValueError(f"未知故障点：{fault}")
    asyncio.run(worker.main())


if __name__ == "__main__":
    main()
