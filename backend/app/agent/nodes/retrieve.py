"""retrieve_knowledge 节点：RAG 知识检索（v2.md §21 / §35）。

只有在 AgentDecision.need_retrieval = true 时才会走到这里 ——
这正是 v2.md §76 想证明的："Agent 能根据问题决定是否使用 RAG，
而不是所有问题都调用 Tool"。
"""

import asyncio

from app.agent.events import emit_event
from app.agent.state import AgentState
from app.core.logging import get_logger
from app.models.agent_event import AgentEventType
from app.rag.retriever import build_context, retrieve, to_references

logger = get_logger(__name__)


async def retrieve_knowledge(state: AgentState) -> dict:
    """按决策生成检索 query，取回知识片段与引用。"""
    ticket_id = state["ticket_id"]
    agent_run_id = state.get("agent_run_id")
    decision = state.get("decision") or {}

    query = _build_query(state, decision)

    emit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=AgentEventType.RETRIEVING_KNOWLEDGE,
        message=f"正在检索售后规则（{query[:30]}）",
    )

    # RAG 只是"辅助证据"，不是主链路：Embedding 服务不可用（key 失效 / 供应商 5xx / 网络抖动）
    # 不该把整张工单搞失败。这里降级为"没有规则依据"继续回答，并如实告诉前端
    # （v2.md Phase 19「RAG失败」）。
    try:
        hits = await asyncio.to_thread(retrieve, query, top_k=4)
    except Exception as exc:  # noqa: BLE001 —— 降级路径必须兜住所有异常
        logger.warning(
            "知识检索失败，已降级为无规则依据：ticket_id=%s error=%s", ticket_id, exc
        )
        emit_event(
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
            event_type=AgentEventType.RETRIEVING_KNOWLEDGE,
            message="知识检索失败，本次回答不含规则依据（已降级）",
        )
        return {
            "retrieved_context": [],
            "references": [],
            "used_rag": False,
            "analysis": state.get("analysis") or "",
        }

    context = build_context(hits)

    if hits:
        titles = "、".join({hit["title"] or "" for hit in hits if hit.get("title")})
        emit_event(
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
            event_type=AgentEventType.RETRIEVING_KNOWLEDGE,
            message=f"命中 {len(hits)} 条规则：{titles}",
        )
    else:
        emit_event(
            agent_run_id=agent_run_id,
            ticket_id=ticket_id,
            event_type=AgentEventType.RETRIEVING_KNOWLEDGE,
            message="未检索到相关知识（NO_RELEVANT_POLICY）",
        )

    logger.info("RAG 检索完成：ticket_id=%s 命中=%s", ticket_id, len(hits))

    return {
        "retrieved_context": hits,
        "references": to_references(hits),
        "used_rag": True,
        # 上下文以文本形式带下去，generate_result 直接拼进 prompt
        "analysis": (state.get("analysis") or "") + (f"\n\n【规则依据】\n{context}" if context else ""),
    }


def _build_query(state: AgentState, decision: dict) -> str:
    """检索 query = 问题类型 + 摘要 + 原文关键词（中文短查询效果往往更好）。"""
    parts = [
        decision.get("issue_type") or "",
        decision.get("summary") or "",
        (state.get("content") or "")[:80],
    ]
    return " ".join(part for part in parts if part).strip()
