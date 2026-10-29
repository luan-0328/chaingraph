"""Worker 执行凭证。每个业务事务先锁定并校验凭证，再允许读写业务数据。"""
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select

from app.core.time import utcnow


class ExecutionLost(RuntimeError):
    """租约失效或执行已被接管；不能继续提交业务动作。"""


@dataclass(frozen=True)
class ExecutionClaim:
    ticket_id: int
    run_id: int
    epoch: int
    worker_id: str


execution_claim: ContextVar[ExecutionClaim | None] = ContextVar("execution_claim", default=None)
execution_session: ContextVar[object | None] = ContextVar("execution_session", default=None)


def assert_execution(db, claim: ExecutionClaim) -> None:
    # 所有执行事务统一按工单 → Run → 订单加锁，避免接管和业务提交交叉。
    from app.models.ticket import Ticket
    from app.models.agent_run import AgentRun, AgentRunStatus

    ticket = db.scalar(select(Ticket).where(Ticket.id == claim.ticket_id).with_for_update())
    run = db.scalar(select(AgentRun).where(AgentRun.id == claim.run_id).with_for_update())
    if (ticket is None or ticket.agent_run_id != claim.run_id or run is None
            or run.status != AgentRunStatus.RUNNING or run.execution_epoch != claim.epoch
            or run.worker_id != claim.worker_id or run.lease_until is None
            or run.lease_until <= utcnow()):
        raise ExecutionLost("执行租约已失效，停止本次执行")
