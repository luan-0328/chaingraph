"""管理后台 DTO（v2.md §45 API 总览里的 /admin/* 接口）。

这些 DTO 是前后端契约的唯一依据（管理员看板 + 用户管理）。

命名约定：DTO 主键叫 user_id，ORM to_dict() 里叫 id（Service 层负责改名）。
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class DashboardStats(BaseModel):
    """GET /admin/stats：管理看板统计。

    ticket_by_status / agent_run_by_status 是"状态 -> 数量"的聚合，
    状态枚举以 models 里的 TicketStatus / AgentRunStatus 为准；
    没有数据的状态可以缺键（前端按缺省 0 渲染），所以用 dict 而不是固定字段。
    """

    ticket_total: int = Field(..., ge=0)
    ticket_by_status: dict[str, int] = Field(default_factory=dict)
    order_total: int = Field(..., ge=0)
    order_paid: int = Field(..., ge=0)
    after_sale_pending: int = Field(..., ge=0)
    agent_run_by_status: dict[str, int] = Field(default_factory=dict)
    product_on_sale: int = Field(..., ge=0)


class AdminUserInfo(BaseModel):
    """管理后台的用户列表行 DTO（比 UserInfo 多 status，用于禁用/启用账号）。"""

    user_id: int
    username: str
    nickname: str | None = None
    role: str
    status: str
    created_at: UtcDatetime


class AdminUserListResponse(BaseModel):
    """GET /admin/users：用户分页列表出参。"""

    items: list[AdminUserInfo] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=0)
    offset: int = Field(..., ge=0)
