"""LangGraph 图：把节点和条件边组装成 Agent 工作流（v2.md §32）。

        START
          ↓
    analyze_ticket ─── AgentDecision
          ↓
    route_after_decision
       ├── need_retrieval ──→ retrieve_knowledge ──┐
       │                            ↓              │
       │                     route_after_retrieve  │
       │                       ├── 要业务数据 ─────┤
       │                       └── 不要 ──────┐    │
       ├── 要业务数据 ──→ execute_tools ──────┤    │
       └── 都不需要 ─────────────────────┐    │    │
                                        ↓    ↓    ↓
                                   generate_result（AgentResult）
                                        ↓
                                 route_after_result
                                   ├── need_human ──→ human_review（interrupt 暂停）
                                   │                        ↓ 人工批准后 resume
                                   └── 不需要 ────────→ execute_action
                                                            ↓
                                     业务规则需人工 → human_review
                                     成功或驳回     → END

关于 Checkpoint：
    编译时必须带 checkpointer，human_review 里的 interrupt() 才能真正"暂停并恢复"。
    没有 checkpointer 时 interrupt 会直接抛错 —— 这也是为什么 HITL 必须配持久化。

为什么节点用"模块属性"而不是直接 import 函数名：
    节点函数通过 `nodes.xxx` 引用（而不是 from ... import xxx）。
    这不是风格问题，而是能力问题：LangGraph 在 compile 时会把节点函数对象**固定**在图里，
    直接 import 的函数名改不动已编译的图，而"改模块属性 + 重新编译"就能换掉节点实现 ——
    这正是并发验收脚本注入"某个节点失败一次"来验证断点续跑所需要的
    （见 app/api/test_hooks.py；生产逻辑完全不受影响，只是 import 形式不同）。
"""

from langgraph.graph import END, START, StateGraph

from app.agent import nodes
from app.agent.edges.conditions import (
    route_after_action,
    route_after_decision,
    route_after_human,
    route_after_result,
    route_after_retrieve,
)
from app.agent.state import AgentState
from app.core.logging import get_logger

logger = get_logger(__name__)

_graph = None


def check_handoff_boundary() -> None:
    """节点边界校验交接请求；只有 worker 携带执行凭证时才访问业务库。"""
    from app.core.execution import execution_claim
    from app.core.database import session_scope
    if execution_claim.get() is not None:
        with session_scope():
            pass


def guarded_node(function):
    """不取消运行中的工具线程；等待节点完成后在进入下一步之前确认交接。"""
    async def invoke(state):
        """在节点前后检查交接边界，等待当前调用退出后再交回人工。"""
        import asyncio
        await asyncio.to_thread(check_handoff_boundary)
        result = await function(state)
        await asyncio.to_thread(check_handoff_boundary)
        return result
    return invoke

# 节点名 → 实现（图与测试注入都用这份映射，避免两处各写一遍字符串）
NODE_FUNCTIONS = {
    "analyze_ticket": lambda: nodes.analyze.analyze_ticket,
    "retrieve_knowledge": lambda: nodes.retrieve.retrieve_knowledge,
    "execute_tools": lambda: nodes.tools.execute_tools,
    "generate_result": lambda: nodes.result.generate_result,
    "human_review": lambda: nodes.human.human_review,
    "execute_action": lambda: nodes.action.execute_action,
}


def build_graph(checkpointer=None):
    """构建并编译 Agent 图。"""
    graph = StateGraph(AgentState)

    # ---- 节点 ----
    for name, resolve in NODE_FUNCTIONS.items():
        graph.add_node(name, guarded_node(resolve()))

    # ---- 边 ----
    graph.add_edge(START, "analyze_ticket")

    graph.add_conditional_edges(
        "analyze_ticket",
        route_after_decision,
        {"retrieve": "retrieve_knowledge", "tools": "execute_tools", "generate": "generate_result"},
    )
    graph.add_conditional_edges(
        "retrieve_knowledge",
        route_after_retrieve,
        {"tools": "execute_tools", "generate": "generate_result"},
    )
    graph.add_edge("execute_tools", "generate_result")

    graph.add_conditional_edges(
        "generate_result",
        route_after_result,
        {"human": "human_review", "action": "execute_action"},
    )
    graph.add_conditional_edges(
        "human_review", route_after_human, {"action": "execute_action"}
    )
    graph.add_conditional_edges(
        "execute_action", route_after_action, {"human": "human_review", "end": END}
    )

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("LangGraph 已编译：analyze → (RAG|Tools|Direct) → result → (HITL) → action")
    return compiled


def set_graph(compiled) -> None:
    """应用启动时注入编译好的图（带 checkpointer）。"""
    global _graph
    _graph = compiled


def get_graph():
    """取当前图；未初始化时给出明确错误（而不是 None 引发难懂的异常）。"""
    if _graph is None:
        raise RuntimeError("Agent 图未初始化：应用启动时应调用 graph.set_graph(build_graph(checkpointer))")
    return _graph
