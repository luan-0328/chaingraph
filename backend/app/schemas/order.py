"""订单 DTO（v2.md §9 订单、§10 订单状态机、§12 支付、§13 物流）。

这些 DTO 是前后端契约的唯一依据。

命名约定：DTO 主键叫 order_id，ORM to_dict() 里叫 id（Service 层负责改名）。

状态字段刻意保持模型里的三状态分离（不要合并成一个 status）：
    payment_status      UNPAID / PAID / REFUNDED
    fulfillment_status  PENDING / PROCESSING / SHIPPED / DELIVERED / COMPLETED / CANCELLED
    after_sale_status   NONE / APPLYING / REFUNDING / REFUNDED
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class OrderItemInfo(BaseModel):
    """订单明细 DTO —— 展示的是**下单时的商品快照**，不跟随商品当前价格变化。"""

    product_id: int
    product_name: str
    product_price: float
    quantity: int = Field(..., ge=1)
    subtotal: float


class PaymentInfo(BaseModel):
    """订单里的支付记录 DTO（v2.md §12）。status: PENDING / SUCCESS / FAILED。"""

    payment_no: str
    amount: float
    status: str
    paid_at: UtcDatetime | None = None


class LogisticsInfo(BaseModel):
    """订单里的物流信息 DTO（v2.md §13）。status: PENDING / IN_TRANSIT / DELIVERED。

    latest_message 是最后一条物流轨迹文案，Agent 判断"物流停滞"时会读它。
    """

    tracking_no: str
    company: str | None = None
    status: str
    latest_message: str | None = None
    last_update_time: UtcDatetime | None = None


class OrderSummary(BaseModel):
    """订单摘要 DTO（订单列表行；不含明细/支付/物流，避免列表接口过重）。"""

    order_id: int
    order_no: str
    total_amount: float
    payment_status: str
    fulfillment_status: str
    after_sale_status: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class OrderDetail(OrderSummary):
    """订单详情 DTO（GET /orders/{id}）：在摘要之上带明细、支付流水与物流。"""

    items: list[OrderItemInfo] = Field(default_factory=list)
    payments: list[PaymentInfo] = Field(default_factory=list)
    logistics: LogisticsInfo | None = None


class OrderListResponse(BaseModel):
    """GET /orders：订单分页列表出参。"""

    items: list[OrderSummary] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=0)
    offset: int = Field(..., ge=0)


class OrderCreateRequest(BaseModel):
    """POST /orders：下单请求体。

    item_ids 不传或为 None = 结算购物车全部商品；
    传了具体行 id = 只结算这些行（必须属于当前用户）。
    """

    item_ids: list[int] | None = Field(default=None, description="要下单的购物车行 id；不传=全部")
