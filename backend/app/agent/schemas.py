"""Agent 的结构化输出模型。

唯一来源是 app/schemas/agent.py（同时被接口契约使用），这里只做别名，
避免"Agent 层和接口层各定义一份、字段慢慢不一致"的经典问题。
"""

from app.schemas.agent import AgentDecisionSchema, AgentResultSchema

# Agent 内部用更短的名字
AgentDecision = AgentDecisionSchema
AgentResult = AgentResultSchema

__all__ = ["AgentDecision", "AgentDecisionSchema", "AgentResult", "AgentResultSchema"]
