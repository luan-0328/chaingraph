"""工单 DTO（v2.md §18 工单、§19 工单状态机、§20 消息、§42 AgentRun、§43 Agent 事件）。

这些 DTO 是前后端契约的唯一依据。

命名约定：DTO 主键叫 ticket_id / message_id / agent_run_id，
ORM to_dict() 里统一叫 id（Service 层负责改名）。

工单状态（TicketStatus）：OPEN / PROCESSING / WAITING_HUMAN / RESOLVED / CLOSED / FAILED
消息来源（sender_type）：USER / AGENT / CUSTOMER_SERVICE / SYSTEM
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class TicketCreate(BaseModel):
    """POST /tickets：用户提交工单（可带订单/售后上下文，便于 Agent 直接查业务数据）。"""

    content: str = Field(..., min_length=1, max_length=2000)
    order_id: int | None = Field(default=None, gt=0)
    after_sale_id: int | None = Field(default=None, gt=0)
    type: str | None = Field(default=None, max_length=16, description="ORDER/LOGISTICS/REFUND/AFTER_SALE/OTHER")
    title: str | None = Field(default=None, max_length=255)


class TicketMessageCreate(BaseModel):
    """POST /tickets/{id}/messages：在工单下追加一条消息。"""

    content: str = Field(..., min_length=1, max_length=2000)


class TicketMessageInfo(BaseModel):
    """工单消息 DTO（用户与客服/Agent 的沟通记录）。

    sender_id 在 sender_type=AGENT / SYSTEM 时为 None（Agent 不是真实用户）。
    """

    message_id: int
    ticket_id: int
    sender_type: str
    sender_id: int | None = None
    content: str
    created_at: UtcDatetime


class AgentEventInfo(BaseModel):
    """Agent 业务事件 DTO（v2.md §43）：落库那份，与 SSE 推送同一份数据。

    event_type 例如 STARTED / ANALYZING / QUERY_ORDER / RETRIEVING_KNOWLEDGE
    / TOOL_CALLING / EXECUTING_REFUND / WAITING_HUMAN / COMPLETED / FAILED。
    """

    event_type: str
    message: str
    created_at: UtcDatetime


class AgentRunInfo(BaseModel):
    """一次 Agent 执行记录 DTO（v2.md §42）。

    decision / result 是模型落库的 JSON 原文：这里保持 dict 不展开成
    AgentDecisionSchema / AgentResultSchema，避免历史数据缺字段时列表接口直接 500
    （演示/回放场景下用 dict 更耐脏），前端按需取字段。

    retry_count  这次执行内部重试了几次（只对可恢复异常重试）
    alive_at     心跳时间（Agent 每个业务事件都会刷新），用于识别僵尸执行
    current_node 此刻正在跑哪个节点（analyze_ticket/…/execute_action），结束或失败时为空
    resumed      true = 这次执行中途失败过，但用断点续跑补上了（复用了已完成的检索/查询结果）
    """

    agent_run_id: int
    thread_id: str
    review_round: int = 0
    review_payload: dict | None = None
    execution_epoch: int = 0
    lease_until: UtcDatetime | None = None
    status: str
    used_rag: bool = False
    used_tool: bool = False
    need_human: bool = False
    retry_count: int = 0
    resumed: bool = False
    current_node: str | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    alive_at: UtcDatetime | None = None
    error: str | None = None
    decision: dict | None = None
    result: dict | None = None


class TicketSummary(BaseModel):
    """工单摘要 DTO（工单列表行 / 客服工作台列表）。

    status       业务流程状态（OPEN/PROCESSING/WAITING_HUMAN/RESOLVED/CLOSED/FAILED）
    agent_status Agent 执行权状态（IDLE/RUNNING/WAITING_HUMAN）—— 两者含义不同：
                 status=PROCESSING 且 agent_status=IDLE 表示客服在处理，Agent 没在跑。
    """

    ticket_id: int
    ticket_no: str
    user_id: int
    title: str | None = None
    content: str
    type: str | None = None
    priority: str
    status: str
    agent_status: str = "IDLE"
    agent_run_id: int | None = None
    assignee_id: int | None = None
    order_id: int | None = None
    after_sale_id: int | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class TicketDetail(TicketSummary):
    """工单详情 DTO（GET /tickets/{id}）。

    messages      全部沟通消息（按时间正序）
    agent_run     最近一次 Agent 执行（工作台顶部展示）
    agent_events  该 run 的业务事件流
    agent_runs    历史执行记录（重试过的旧 run 也保留，见 v2.md §66 可追溯）
    """

    messages: list[TicketMessageInfo] = Field(default_factory=list)
    agent_run: AgentRunInfo | None = None
    agent_events: list[AgentEventInfo] = Field(default_factory=list)
    agent_runs: list[AgentRunInfo] = Field(default_factory=list)


class TicketListResponse(BaseModel):
    """GET /tickets：工单分页列表出参。"""

    items: list[TicketSummary] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=0)
    offset: int = Field(..., ge=0)


class TicketResolveRequest(BaseModel):
    """POST /tickets/{id}/resolve：客服（或 Agent 代答后由客服确认）给出解决方案。"""

    solution: str = Field(..., min_length=1, max_length=1000)


class TicketCloseRequest(BaseModel):
    """POST /tickets/{id}/close：关闭工单（解决后等用户确认/客服直接关闭）。"""

    comment: str | None = Field(default=None, max_length=1000)


class TicketAssignRequest(BaseModel):
    """POST /tickets/{id}/assign：客服领取工单；assignee_id 为 None 表示取消领取。

    不传 assignee_id 时由 Service 填当前登录客服的 user_id（"领取我"），
    显式传 null 才是取消领取 —— Service 用 exclude_unset 区分这两种情况。
    """

    assignee_id: int | None = Field(default=None, gt=0)
