"""analyze_ticket 节点：LLM 结构化分析 → AgentDecision（v2.md §17.1 / §30）。

这是整张图的入口：它决定后面走 RAG、走 Tools 还是直接生成结论。
关键点（v2.md §18/§20）：
    **不让模型返回一段自由文本当流程控制依据**，而是输出 Pydantic 结构化决策；
    图根据 decision 的布尔开关走条件边 —— 流程仍然由程序定义。
"""

from app.agent.events import emit_event
from app.agent.llm import ainvoke_structured
from app.agent.prompts import ANALYZE_SYSTEM_PROMPT
from app.agent.schemas import AgentDecision
from app.agent.state import AgentState
from app.core.exceptions import AgentExecutionError
from app.core.logging import get_logger
from app.models.agent_event import AgentEventType

logger = get_logger(__name__)


async def analyze_ticket(state: AgentState) -> dict:
    """分析工单，产出结构化决策。"""
    ticket_id = state["ticket_id"]
    agent_run_id = state.get("agent_run_id")

    emit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=AgentEventType.ANALYZING,
        message="正在分析工单",
    )

    conversation = _format_conversation(state)
    user_prompt = (
        f"工单编号：{state.get('ticket_id')}\n"
        f"关联订单 ID：{state.get('order_id') or '（未关联）'}\n"
        f"关联售后单 ID：{state.get('after_sale_id') or '（无）'}\n\n"
        f"【工单内容】\n{state.get('content', '')}\n\n"
        f"【最近对话】\n{conversation}\n\n"
        f"请给出结构化决策。"
    )

    try:
        decision: AgentDecision = await ainvoke_structured(
            AgentDecision,
            [
                {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            what=f"工单分析（ticket_id={ticket_id}）",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze_ticket 调用 LLM 失败：ticket_id=%s", ticket_id)
        raise AgentExecutionError(f"工单分析失败：{exc}") from exc

    logger.info(
        "决策结果：ticket_id=%s type=%s retrieval=%s order=%s logistics=%s after_sale=%s auto=%s human=%s",
        ticket_id,
        decision.issue_type,
        decision.need_retrieval,
        decision.need_order_query,
        decision.need_logistics_query,
        decision.need_after_sale_query,
        decision.can_auto_process,
        decision.need_human,
    )

    emit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=AgentEventType.DECIDED,
        message=f"识别为「{decision.issue_type}」：{decision.summary}",
    )

    return {
        "decision": decision.model_dump(),
        "analysis": decision.summary,
        "need_human": bool(decision.need_human),
    }


def _format_conversation(state: AgentState) -> str:
    """把工单消息格式化成给模型的对话文本（最多取最近 10 条）。"""
    messages = state.get("conversation") or []
    if not messages:
        return "（暂无更多对话）"

    role_names = {
        "USER": "用户",
        "AGENT": "Agent",
        "CUSTOMER_SERVICE": "客服",
        "SYSTEM": "系统",
    }
    lines = [
        f"- {role_names.get(message.get('sender_type'), message.get('sender_type'))}：{message.get('content')}"
        for message in messages[-10:]
    ]
    return "\n".join(lines)
