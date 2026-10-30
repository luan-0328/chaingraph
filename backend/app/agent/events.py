"""Agent 业务事件：落库 + 实时推送（v2.md §43 / §44）。

节点里统一调用 emit_event(...)：
    - 写一条 agent_run_events（业务过程可回溯，客服工作台能看到）
    - 推一份到 SSE 总线（前端 ProcessTimeline 实时展示）
    - 顺带刷新 AgentRun 的心跳（见下）

只记录**业务事件**（正在查询订单 / 正在检索规则 / 执行退款 / 等待人工），
绝不记录模型思维链。

每个事件用独立短事务写库：Agent 执行时间较长，不应该把整个 run 包在一个长事务里。

活跃时间与租约
--------------
事件会刷新 alive_at，供观察最近活动。执行权由独立心跳续租和 lease_until 判断，
不能仅凭最后一条事件推断租约仍有效。异步节点通过 aemit_event 在线程中写库，
同步工具使用 emit_event；两者均等待事务结束。带幂等键的审计写入失败会向上抛出。
"""

import asyncio

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.agent_event import AgentEventType
from app.repositories import agent_run_repository
from app.agent.progress import progress_bus

logger = get_logger(__name__)


def emit_event(
    *,
    agent_run_id: int | None,
    ticket_id: int,
    event_type: str,
    message: str,
    dedup_key: str | None = None,
) -> dict:
    """记录并广播一个 Agent 业务事件。"""
    event = {
        "type": "event",
        "event_type": event_type,
        "message": message,
        "agent_run_id": agent_run_id,
        "ticket_id": ticket_id,
    }

    if agent_run_id:
        try:
            with session_scope() as db:
                saved = agent_run_repository.add_event(
                    db, agent_run_id=agent_run_id, event_type=event_type, message=message,
                    dedup_key=dedup_key,
                )
                agent_run_repository.heartbeat(db, agent_run_id, utcnow())
            event["event_id"] = saved["id"]
            if not saved["inserted"]:
                return event
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent 事件落库失败（dedup_key=%s）：%s", dedup_key, exc)
            if dedup_key is not None:
                raise  # 幂等审计失败必须可重试，不能只推送一条未持久化的成功事件。

    # worker 的实时进度由 API 查询持久化事件提供，不积累进程内频道。
    from app.core.execution import execution_claim
    if execution_claim.get() is None:
        progress_bus.publish(ticket_id, event)
    return event


async def aemit_event(**kwargs) -> dict:
    """在线程中完成事件事务；异步节点等待落库，但不阻塞其他工单的调度。"""
    return await asyncio.to_thread(emit_event, **kwargs)


def emit_done(*, ticket_id: int, agent_run_id: int | None, status: str, message: str, payload: dict | None = None) -> None:
    """处理结束（成功/失败/等待人工）时告诉前端"可以刷新了"。"""
    progress_bus.publish(
        ticket_id,
        {
            "type": "done",
            "status": status,
            "message": message,
            "agent_run_id": agent_run_id,
            "ticket_id": ticket_id,
            **(payload or {}),
        },
    )


def emit_error(
    *, ticket_id: int, agent_run_id: int | None, message: str, detail: str | None = None
) -> None:
    """推送一条"流级错误"帧，**只推不落库**。

    和 `emit_event` 的区别很重要，别混用：

        emit_event  → 业务步骤（"正在查询订单"），落 agent_run_events 且可回放
        emit_error  → 这次推送/执行出问题了，只让前端当场知道；
                      真正要留档的失败原因由 `_mark_run_failed` 写进
                      AgentRun.error 与 FAILED 事件

    为什么不落库：它常常是"同一条失败"的第二个出口（第一个已经落库了），
    再落一条只会让客服看到重复的失败记录。
    """
    progress_bus.publish(
        ticket_id,
        {
            "type": "error",
            "message": message,
            "detail": detail,
            "agent_run_id": agent_run_id,
            "ticket_id": ticket_id,
        },
    )


__all__ = ["aemit_event", "AgentEventType", "emit_done", "emit_error", "emit_event"]
