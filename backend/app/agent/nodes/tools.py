"""execute_tools 节点：按 AgentDecision 的开关调用业务工具（v2.md §25 / §36）。

注意这里的设计：**不是让模型自由决定调哪个工具**，而是图根据结构化决策
（need_order_query / need_logistics_query / need_after_sale_query）确定性地调用。
这样"要不要查、查什么"可控可测（v2.md §20：流程由程序定义）。
"""

import asyncio

from app.agent.events import emit_event
from app.agent.state import AgentState
from app.core.logging import get_logger
from app.models.agent_event import AgentEventType
from app.tools import QUERY_TOOLS

logger = get_logger(__name__)

# 决策开关 → (工具名, 事件类型, 事件文案)
TOOL_PLAN = (
    ("need_order_query", "query_order", AgentEventType.QUERY_ORDER, "正在查询订单信息"),
    ("need_logistics_query", "query_logistics", AgentEventType.QUERY_LOGISTICS, "正在查询物流信息"),
    ("need_after_sale_query", "query_after_sale", AgentEventType.QUERY_AFTER_SALE, "正在查询售后信息"),
)


async def execute_tools(state: AgentState) -> dict:
    """执行查询类工具，把结果收集进 state.tool_results。"""
    ticket_id = state["ticket_id"]
    agent_run_id = state.get("agent_run_id")
    decision = state.get("decision") or {}
    order_id = state.get("order_id")
    user_id = state["user_id"]

    results: list[dict] = []

    for flag, tool_name, event_type, message in TOOL_PLAN:
        if not decision.get(flag):
            continue
        if order_id is None:
            logger.info("决策要求 %s 但工单没有关联订单，跳过", tool_name)
            continue

        results.append(
            await _invoke(tool_name, {"order_id": order_id, "user_id": user_id},
                    event_type=event_type, message=message,
                    ticket_id=ticket_id, agent_run_id=agent_run_id)
        )

    # 长期记忆：最近的历史工单（几乎所有场景都有用，成本很低）
    results.append(
        await _invoke("query_ticket_history", {"user_id": user_id, "limit": 5},
                event_type=AgentEventType.TOOL_CALLING, message="正在查询历史工单",
                ticket_id=ticket_id, agent_run_id=agent_run_id)
    )

    logger.info("工具执行完成：ticket_id=%s 调用=%s", ticket_id, [r["tool"] for r in results])

    return {"tool_results": results, "used_tool": True}


async def _invoke(
    tool_name: str,
    args: dict,
    *,
    event_type: str,
    message: str,
    ticket_id: int,
    agent_run_id: int | None,
) -> dict:
    """调用一个工具并记录事件。工具内部已经把业务异常转成结构化错误。"""
    emit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=event_type,
        message=message,
    )

    tool = QUERY_TOOLS[tool_name]
    try:
        output = await asyncio.to_thread(tool.invoke, args)
    except Exception as exc:  # noqa: BLE001 —— 工具本身炸了也不能让整张图挂掉
        logger.exception("工具 %s 执行异常", tool_name)
        output = {"success": False, "error": "TOOL_EXECUTION_ERROR", "message": str(exc)[:200]}

    return {"tool": tool_name, "args": args, "output": output}
