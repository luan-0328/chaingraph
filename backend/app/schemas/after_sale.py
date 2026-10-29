"""售后与退款 DTO（v2.md §15 售后、§16 退款）。

这些 DTO 是前后端契约的唯一依据。

区分两个概念（v2 的核心设计）：
    AfterSale  = 业务对象（用户申请的退款/退货/换货单据）
    RefundInfo = 资金流水（钱实际退回的记录，一次售后可能有多条重试记录）

命名约定：DTO 主键叫 after_sale_id，ORM to_dict() 里叫 id（Service 层负责改名）。
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class AfterSaleCreate(BaseModel):
    """POST /after-sales：用户申请售后。

    type: REFUND / RETURN / EXCHANGE / OTHER（V1 只完整实现 REFUND）；
    amount 不传 = 按订单金额全额退款，由 Service 计算并受退款规则校验。
    """

    order_id: int = Field(..., gt=0)
    type: str = Field(default="REFUND", max_length=16)
    reason: str = Field(..., min_length=1, max_length=500)
    amount: float | None = Field(default=None, gt=0, description="不传=按订单金额全额")


class RefundInfo(BaseModel):
    """退款流水 DTO（v2.md §16）。status: PENDING / SUCCESS / FAILED。

    approved_by 为 None 表示系统自动退款，否则是审批人（客服/管理员）的 user_id。
    """

    refund_no: str
    amount: float
    status: str
    reason: str | None = None
    approved_by: int | None = None
    created_at: UtcDatetime
    completed_at: UtcDatetime | None = None


class AfterSaleInfo(BaseModel):
    """售后单 DTO（列表与详情共用）。

    status: APPLYING / PROCESSING / APPROVED / REJECTED / COMPLETED / CANCELLED。
    """

    after_sale_id: int
    approval_context: dict | None = None
    after_sale_no: str
    order_id: int
    type: str
    status: str
    amount: float
    reason: str | None = None
    refunds: list[RefundInfo] = Field(default_factory=list)
    created_at: UtcDatetime
    updated_at: UtcDatetime
    completed_at: UtcDatetime | None = None


class AfterSaleListResponse(BaseModel):
    """GET /after-sales：售后分页列表出参。"""

    items: list[AfterSaleInfo] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=0)
    offset: int = Field(..., ge=0)


class AfterSaleCancelRequest(BaseModel):
    """POST /after-sales/{id}/cancel：用户撤销售后申请。reason 仅作留痕，可空。"""

    reason: str | None = Field(default=None, max_length=500)


class RefundApprovalRequest(BaseModel):
    after_sale_id: int = Field(..., gt=0)
    expected_run_id: int | None = Field(default=None, gt=0)
    review_round: int | None = Field(default=None, gt=0)
    order_snapshot: dict
    comment: str | None = Field(default=None, max_length=500)


class RefundApplyResponse(BaseModel):
    """POST /after-sales/{id}/refund：退款申请结果。

    为什么不能只返回售后单？
        退款有两种结局：**当场自动退掉**，或者**规则不允许自动退、转人工审核**。
        前端必须能区分这两种情况，才能给出"退款已到账"还是"已提交人工审核"的提示。
    """

    after_sale: AfterSaleInfo
    executed: bool = Field(..., description="是否已自动执行退款")
    need_human: bool = Field(..., description="是否需要人工审核")
    reason: str = Field(..., description="规则判定说明（给人看）")
    refund: RefundInfo | None = Field(default=None, description="自动退款成功时的流水")
