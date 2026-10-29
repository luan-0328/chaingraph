"""条件边：根据 State 决定图下一步走哪（v2.md §20）。

    route_after_decision   analyze 之后：要知识？要业务数据？还是直接出方案
    route_after_retrieve   RAG 之后：还需要业务数据吗
    route_after_result     出方案之后：需要人工吗
    route_after_human      人工审批之后：统一进 execute_action（驳回时该节点内部会跳过执行）

**流程由程序定义，不是模型随便跳节点** —— 这里的每个判断读的都是结构化 State 字段。
"""

from app.agent.state import AgentState


def _needs_business_query(decision: dict) -> bool:
    """决策里只要有一个"要查业务数据"的开关为真，就需要走工具节点。

    三个开关是分开的（订单/物流/售后），因为模型该被允许只查它需要的那个 ——
    全部合并成一个 `need_query` 会让 Agent 每次退款都顺手查一遍物流。
    """
    return bool(
        decision.get("need_order_query")
        or decision.get("need_logistics_query")
        or decision.get("need_after_sale_query")
    )


def route_after_decision(state: AgentState) -> str:
    """analyze 之后分流：先 RAG、再工具、最后直接出方案。

    顺序刻意的：**先检索规则，再拿业务数据**。
    因为"该不该查这个数据"本身往往取决于规则（例如超过 7 天就不用查退货资格了），
    先拿规则能让后面的查询更有针对性；反过来则会白查一堆用不上的数据。

    注意返回的是图的**节点名**（"retrieve"/"tools"/"generate"），
    映射关系在 graph.py 的 `add_conditional_edges` 里 —— 改名字要两边一起改。
    """
    decision = state.get("decision") or {}

    if decision.get("need_retrieval"):
        return "retrieve"
    if _needs_business_query(decision):
        return "tools"
    return "generate"


def route_after_retrieve(state: AgentState) -> str:
    """RAG 之后：决策还要求业务数据就去工具节点，否则直接出方案。

    只读 `decision` 不看 RAG 结果 —— 检索到没检索到都不该影响
    "要不要查订单"这个决策，那是模型在 analyze 时定的。
    """
    decision = state.get("decision") or {}
    return "tools" if _needs_business_query(decision) else "generate"


def route_after_result(state: AgentState) -> str:
    """出方案之后：需要人工就挂起（human_review），否则直接执行动作。

    `need_human` 由 **AgentResult** 决定（模型层），但真正挂起与否还受
    `edges` 之外的因素影响：HITL 的 interrupt 在 human_review 节点内部。
    """
    return "human" if state.get("need_human") else "action"


def route_after_human(state: AgentState) -> str:
    """人工审批之后**统一**进 execute_action。

    为什么批准和驳回走同一条边：把"驳回"也在图里分个岔，
    会多出三个节点（驳回-写消息、驳回-结单…）却没有多出任何信息 ——
    execute_action 内部看一眼 `human_approved is False` 就跳过了业务动作，
    一个分支比一条边更省事，也更容易保证"驳回绝不执行退款"。
    """
    # 批准 → 继续执行；驳回 → 也走 execute_action（它内部判断 human_approved=False 后跳过业务动作）
    return "action"


def route_after_action(state: AgentState) -> str:
    """业务校验要求人工时，必须留下真正可恢复的 interrupt。"""
    return "human" if state.get("status") == "WAITING_HUMAN" else "end"
