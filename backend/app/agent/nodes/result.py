"""generate_result 节点：把事实 + 知识 + 对话合成结构化处理结果（v2.md §27 / §31）。

输入：工单内容、最近对话、业务工具结果、RAG 知识片段
输出：AgentResult（issue_type / summary / analysis / solution / references / tool_results /
      business_action / need_human）

它决定了后面走"自动执行"还是"转人工"。
"""

import asyncio
import json

from app.agent.events import emit_event
from app.agent.llm import ainvoke_structured
from app.agent.prompts import RESULT_SYSTEM_PROMPT
from app.agent.schemas import AgentResult
from app.agent.state import AgentState
from app.core.exceptions import AgentExecutionError
from app.core.logging import get_logger
from app.models.agent_event import AgentEventType
from app.tools import QUERY_TOOLS

logger = get_logger(__name__)


async def generate_result(state: AgentState) -> dict:
    """生成最终处理结果。"""
    ticket_id = state["ticket_id"]
    agent_run_id = state.get("agent_run_id")

    # 每条路由都会经过本节点；纯咨询跳过 execute_tools 时也能读取历史记忆。
    emit_event(agent_run_id=agent_run_id, ticket_id=ticket_id,
               event_type=AgentEventType.TOOL_CALLING, message="正在读取该用户的历史处理摘要")
    args = {"user_id": state["user_id"], "limit": 3, "exclude_ticket_id": ticket_id}
    try:
        memory = await asyncio.to_thread(QUERY_TOOLS["query_user_memory"].invoke, args)
    except Exception:
        logger.warning("历史记忆读取失败，继续处理当前工单：ticket_id=%s", ticket_id, exc_info=True)
        memory = {"success": False, "error": "MEMORY_UNAVAILABLE", "message": "历史记忆暂不可用"}
    tool_results = [item for item in (state.get("tool_results") or []) if item["tool"] != "query_user_memory"]
    tool_results.append({"tool": "query_user_memory", "args": args, "output": memory})
    state = {**state, "tool_results": tool_results}

    facts = _format_facts(state)
    knowledge = _format_knowledge(state)

    user_prompt = (
        # 工单号放在最前面：既是排查日志时的抓手，也让"模型/桩"始终知道自己正在处理哪张工单
        f"工单编号：{ticket_id}\n\n"
        f"【工单内容】\n{state.get('content', '')}\n\n"
        f"【最近对话】\n{_format_conversation(state)}\n\n"
        f"【业务数据（系统实际查到的，未经查到的不要臆造）】\n{facts}\n\n"
        f"【售后规则知识（RAG 检索结果）】\n{knowledge}\n\n"
        f"请给出结构化处理结果。"
    )

    try:
        result: AgentResult = await ainvoke_structured(
            AgentResult,
            [
                {"role": "system", "content": RESULT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            what=f"生成处理结果（ticket_id={ticket_id}）",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_result 调用 LLM 失败：ticket_id=%s", ticket_id)
        raise AgentExecutionError(f"生成处理结果失败：{exc}") from exc

    # 把工具结果与引用补进结果（模型不需要复述这些原始数据）
    payload = result.model_dump()
    payload["references"] = state.get("references") or []
    payload["tool_results"] = [
        {"tool": item["tool"], "ok": bool((item.get("output") or {}).get("success"))}
        for item in state.get("tool_results") or []
    ]
    # 只有明确的咨询答复或受支持的业务动作，才允许进入自动完成路径。
    decision = state.get("decision") or {}
    valid_answer = (result.resolution_kind == "ANSWER"
                    and decision.get("request_kind") == "CONSULTATION"
                    and not result.business_action)
    valid_action = result.resolution_kind == "ACTION" and bool(result.business_action)
    missing_solution = not result.solution.strip()
    need_human = (bool(result.need_human) or bool(decision.get("need_human"))
                  or missing_solution or not (valid_answer or valid_action))
    failed_query = any(item["tool"] != "query_user_memory"
                       and not (item.get("output") or {}).get("success")
                       for item in tool_results)
    missing_rules = decision.get("need_retrieval") and not state.get("retrieved_context")
    need_human = bool(need_human or failed_query or missing_rules)
    payload["need_human"] = need_human
    if missing_solution:
        payload["solution"] = "未生成有效处理方案，请人工核实用户诉求"

    emit_event(
        agent_run_id=agent_run_id,
        ticket_id=ticket_id,
        event_type=AgentEventType.RESULT_GENERATED,
        message=f"已生成处理方案：{result.solution[:60]}",
    )

    logger.info(
        "处理结果：ticket_id=%s action=%s need_human=%s",
        ticket_id,
        result.business_action or "(无)",
        result.need_human,
    )

    return {
        "result": payload,
        "analysis": result.analysis or state.get("analysis"),
        "need_human": need_human,
        "business_action": result.business_action or None,
        "tool_results": tool_results,
        "used_tool": True,
    }

def _format_facts(state: AgentState) -> str:
    """把工具结果整理成模型易读的文本。"""
    results = state.get("tool_results") or []
    if not results:
        return "（没有查询到业务数据）"

    blocks: list[str] = []
    for item in results:
        output = item.get("output") or {}
        if output.get("success"):
            data = output.get("data", output)
            blocks.append(
                f"- {item['tool']}：{json.dumps(data, ensure_ascii=False, default=str)[:1200]}"
            )
        else:
            blocks.append(
                f"- {item['tool']}：查询失败（{output.get('error')}）{output.get('message', '')}"
            )
    return "\n".join(blocks)


def _format_knowledge(state: AgentState) -> str:
    """把 RAG 命中拼进 prompt，**检索不到时给一句明确的兜底指令**。

    那句"没有检索到相关规则；如果规则关键，请要求人工确认而不是自行判断"
    是刻意写进 prompt 的：没有规则依据时，模型最危险的行为是
    "凭常识编一条退款规则" —— 与其等它编，不如明确要求它转人工。
    """
    hits = state.get("retrieved_context") or []
    if not hits:
        return "（没有检索到相关规则；如果规则关键，请要求人工确认而不是自行判断）"

    blocks = []
    for index, hit in enumerate(hits, start=1):
        blocks.append(
            f"[{index}] {hit.get('title')}（{hit.get('source')}）\n{hit.get('content')}"
        )
    return "\n\n".join(blocks)


def _format_conversation(state: AgentState) -> str:
    """把工单对话拼进 prompt（**只取最近 10 条**）。

    截断是为了控制 prompt 长度：工单可能来回几十轮，
    全塞进去既贵又会把最重要的"最新诉求"稀释掉。

    sender_type 翻成中文名：模型对"客服：…"的理解比对"CUSTOMER_SERVICE：…"更稳，
    这一点在中文业务场景里实测有差别 —— 它也更容易正确区分"用户说的"和"客服说的"。
    """
    messages = state.get("conversation") or []
    if not messages:
        return "（暂无更多对话）"
    names = {"USER": "用户", "AGENT": "Agent", "CUSTOMER_SERVICE": "客服", "SYSTEM": "系统"}
    return "\n".join(
        f"- {names.get(m.get('sender_type'), m.get('sender_type'))}：{m.get('content')}"
        for m in messages[-10:]
    )
