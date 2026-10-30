"""human_review 节点：HITL 的暂停点（v2.md §33 / §40）。

interrupt() 会做三件事：
    1. 把当前 State 写进 Checkpoint（thread_id 为键）；
    2. 抛出一个特殊的暂停信号，图执行到此为止，请求返回；
    3. 之后用同一个 thread_id + Command(resume=...) 恢复，interrupt() 的**返回值**
       就是人工给出的决定。

所以这一行 `decision = interrupt(payload)` 在"第一次执行"和"恢复执行"时
表现完全不同 —— 第一次挂起，第二次拿到审批结果继续往下走。
"""

import asyncio

from langgraph.types import interrupt

from app.agent.events import aemit_event
from app.agent.state import AgentState
from app.core.logging import get_logger
from app.models.agent_event import AgentEventType

logger = get_logger(__name__)


async def human_review(state: AgentState) -> dict:
    """暂停等待人工审批，恢复后返回审批结果。"""
    ticket_id = state["ticket_id"]
    agent_run_id = state.get("agent_run_id")
    result = state.get("result") or {}
    review_round = state.get("human_review_round", 0) + 1
    key = f"run:{agent_run_id}:review:{review_round}" if agent_run_id is not None else None

    payload = {
        "reason": (state.get("action_result") or {}).get("message") or "Agent 无法安全自动处理，需要人工确认",
        "issue_type": result.get("issue_type"),
        "summary": result.get("summary"),
        "analysis": result.get("analysis"),
        "solution": result.get("solution"),
        "business_action": result.get("business_action"),
        "references": state.get("references") or [],
        "ticket_id": ticket_id,
        "agent_run_id": agent_run_id,
        "action_result": state.get("action_result"),
    }

    from app.services.agent_task_service import save_review
    payload = await asyncio.to_thread(save_review, state, payload, review_round)
    review_round = payload["review_round"]
    key = f"run:{agent_run_id}:review:{review_round}" if agent_run_id is not None else None

    await aemit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=AgentEventType.WAITING_HUMAN,
        message="已提交人工审核，等待客服确认",
        dedup_key=f"{key}:waiting" if key else None,
    )

    logger.info("Agent 暂停等待人工审批：ticket_id=%s", ticket_id)

    # ---- 图在这里挂起；恢复时 decision 就是 Command(resume=...) 传进来的内容 ----
    decision = interrupt(payload)

    approved = bool((decision or {}).get("approved"))
    comment = (decision or {}).get("comment")
    operator_id = (decision or {}).get("operator_id")

    await aemit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=(
            AgentEventType.HUMAN_APPROVED if approved else AgentEventType.HUMAN_REJECTED
        ),
        message=(
            f"客服已批准处理方案{f'：{comment}' if comment else ''}"
            if approved
            else f"客服已驳回处理方案{f'：{comment}' if comment else ''}"
        ),
        dedup_key=f"{key}:decision" if key else None,
    )

    return {
        "human_approved": approved,
        "human_comment": comment,
        "human_operator_id": operator_id,
        "human_review_round": (decision or {}).get("review_round", review_round),
    }
