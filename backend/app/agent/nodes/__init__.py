"""nodes：LangGraph 节点。

    analyze.py    LLM 结构化分析 → AgentDecision
    retrieve.py   RAG 知识检索 → retrieved_context / references
    tools.py      按决策调用业务查询工具 → tool_results
    result.py     LLM 结构化输出 AgentResult
    human.py      interrupt() 暂停等待人工审批（HITL）
    action.py     执行处理方案（apply_refund 等）并更新工单

节点规矩：输入 State、输出"要合并进 State 的字段"；
不在节点里写 SQL，业务改动一律通过 tools → service。

子模块同时以"模块"形式暴露（`from app.agent import nodes` 后用
`nodes.retrieve.retrieve_knowledge`），因为 graph.py 需要在**重编译时**
重新取一次节点函数 —— 这样测试才能把某个节点换成"会失败一次"的包装，
用来验证断点续跑（见 app/api/test_hooks.py）。
"""

from app.agent.nodes import action, analyze, human, result, retrieve, tools
from app.agent.nodes.action import execute_action
from app.agent.nodes.analyze import analyze_ticket
from app.agent.nodes.human import human_review
from app.agent.nodes.result import generate_result
from app.agent.nodes.retrieve import retrieve_knowledge
from app.agent.nodes.tools import execute_tools

__all__ = [
    "action",
    "analyze",
    "analyze_ticket",
    "execute_action",
    "execute_tools",
    "generate_result",
    "human",
    "human_review",
    "result",
    "retrieve",
    "retrieve_knowledge",
    "tools",
]
