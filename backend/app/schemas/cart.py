"""购物车 DTO（v2.md §8 购物车）。

这些 DTO 是前后端契约的唯一依据。

命名约定：DTO 主键叫 item_id，ORM to_dict() 里叫 id（Service 层负责改名）。
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class CartItemAdd(BaseModel):
    """POST /cart/items：加入购物车（同商品重复加购由 Service 累加数量）。"""

    product_id: int = Field(..., gt=0)
    quantity: int = Field(default=1, ge=1, le=999)


class CartItemUpdate(BaseModel):
    """PUT /cart/items/{item_id}：修改购物车行的数量。"""

    quantity: int = Field(..., ge=1, le=999)


class CartItemInfo(BaseModel):
    """购物车行 DTO。

    product_* / price / stock 由 Service 关联商品表后填充：
    商品已被删除时这三个字段为 None（前端据此显示"商品已下架/失效"）。
    subtotal 同理为 None（无法计算）。
    """

    item_id: int
    product_id: int
    product_name: str | None = None
    price: float | None = None
    stock: int | None = None
    product_status: str | None = None
    quantity: int = Field(..., ge=1)
    subtotal: float | None = None
    added_at: UtcDatetime


class CartResponse(BaseModel):
    """GET /cart：购物车出参，附带合计数量与合计金额（失效商品不计入金额）。"""

    items: list[CartItemInfo] = Field(default_factory=list)
    total_quantity: int = Field(..., ge=0)
    total_amount: float = Field(..., ge=0)


class CartCheckoutRequest(BaseModel):
    """POST /orders 的第一个入口：把购物车行转成订单。

    item_ids 为空列表表示"结算购物车全部商品"；
    传入的行 id 必须属于当前用户，且商品必须在上架状态（由 Service 校验）。
    """

    item_ids: list[int] = Field(default_factory=list, description="要结算的购物车行 id；空=全选")
