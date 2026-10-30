"""Agent DTO（v2.md §28 AgentState、§30 AgentDecision、§31 AgentResult、§45 API 总览）。

这些 DTO 是前后端契约的唯一依据。

AgentDecision / AgentResult 既是 LLM 结构化输出的校验模型，也是接口出参：
    AgentDecisionSchema  模型"打算怎么做"（要不要检索、要不要查业务、要不要人工）
    AgentResultSchema    模型"最终结论"（分析、方案、引用、工具结果、业务动作）

注意：这两个 Schema 校验的是**模型输出**，所以文本字段只约束"必填"、不约束长度 ——
模型偶尔返回空串时应当由 Agent 层的重试/降级逻辑处理，
而不是让 Pydantic 校验直接抛错把整次 run 打成 FAILED。
"""

from typing import Literal

from pydantic import BaseModel, Field

# 问题类型：用 Literal 把模型的输出**约束死**（v2.md §83.12：Structured Output 负责约束 Agent 决策）
# 否则模型可能返回中文或近义词（实测会返回"退款"），导致后续按类型路由/回填工单时匹配不上。
IssueType = Literal["ORDER", "LOGISTICS", "REFUND", "AFTER_SALE", "OTHER"]

# 允许模型请求执行的业务动作：只有这两个，其余一律视为"不需要动作"
BusinessAction = Literal["apply_refund", "cancel_order", ""]


class AgentDecisionSchema(BaseModel):
    """AgentDecision（v2.md §30）：Agent 处理前的结构化决策，决定走哪些节点/工具。"""

    issue_type: IssueType = Field(..., description="问题类型，必须是给定取值之一")
    summary: str = Field(..., description="对用户问题的简短复述")
    request_kind: Literal["CONSULTATION", "ACTION", "UNKNOWN"] = "UNKNOWN"
    request_human: bool = Field(default=False, description="用户明确希望真人接手并继续交流；不等同于业务方案需人工审批")
    need_retrieval: bool = False
    need_order_query: bool = False
    need_logistics_query: bool = False
    need_after_sale_query: bool = False
    can_auto_process: bool = False
    need_human: bool = False


class AgentResultSchema(BaseModel):
    """AgentResult（v2.md §31）：Agent 的结构化结果，客服工作台与用户回复都由它渲染。

    references    RAG 命中的知识片段（title/source/category/content/score）
    tool_results  调用过的业务工具返回（订单/物流/售后查询结果）
    business_action 需要系统执行的业务动作（受 Literal 约束，模型不能自己发明动作）
    """

    issue_type: IssueType
    summary: str
    analysis: str
    solution: str
    resolution_kind: Literal["ANSWER", "ACTION", "NEED_INFO", "HUMAN"] = "HUMAN"
    references: list[dict] = Field(default_factory=list)
    tool_results: list[dict] = Field(default_factory=list)
    business_action: BusinessAction = ""
    need_human: bool = False


class AgentProcessResponse(BaseModel):
    """POST /tickets/{id}/agent/process：触发 Agent 处理（同步返回）出参。

    两种情形，前端只看 `status` 就能分支：

    ① 本次真的执行了 → status 是 **AgentRun** 的状态：
       PENDING/PROCESSING/WAITING_HUMAN/COMPLETED/FAILED/REJECTED
    ② 同工单已有执行在跑（并发抢占失败）→ status = ALREADY_RUNNING，
       且 agent_run_id 指向**当前那次**执行（不会新建 AgentRun）。
       此时可以直接连 SSE（GET /tickets/{id}/stream）看那一次的执行进度。

    agent_status 是工单上的执行权状态（IDLE/RUNNING/WAITING_HUMAN），
    used_existing_run=True 表示"这次请求复用了已有执行，没有产生新的 AgentRun"。
    """

    ticket_id: int
    agent_run_id: int
    thread_id: str
    status: str
    need_human: bool
    decision: AgentDecisionSchema | None = None
    result: AgentResultSchema | None = None
    agent_status: str | None = Field(default=None, description="IDLE / RUNNING / WAITING_HUMAN")
    used_existing_run: bool = Field(default=False, description="是否复用了已有执行（未新建 AgentRun）")
    message: str | None = Field(default=None, description="给人看的一句话说明")


class AgentApprovalRequest(BaseModel):
    """POST /agent/runs/{id}/approve：客服批准 Agent 停在人工确认点的动作。"""

    comment: str | None = Field(default=None, max_length=1000)
    expected_run_id: int = Field(..., gt=0)
    review_round: int = Field(..., gt=0)


class AgentRejectRequest(BaseModel):
    """POST /agent/runs/{id}/reject：客服拒绝 Agent 的动作（run 置为 REJECTED）。"""

    reason: str | None = Field(default=None, max_length=1000)
    expected_run_id: int = Field(..., gt=0)
    review_round: int = Field(..., gt=0)


class AgentProgressEvent(BaseModel):
    """SSE 进度事件（GET /tickets/{id}/agent/stream）：与落库事件同源，实时推给前端。

    type      流帧类型："event"（过程事件）/ "done"（结束）/ "error"（失败）
    at        事件时间的 ISO 字符串，由 SSE 层生成，前端只做展示
    """

    type: str = Field(..., description="event / done / error")
    event_type: str | None = None
    message: str
    agent_run_id: int | None = None
    thread_id: str | None = None
    status: str | None = None
    ticket_id: int | None = None
    at: str | None = None
