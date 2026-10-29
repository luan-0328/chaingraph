"""商品 DTO（v2.md §7 商品）。

这些 DTO 是前后端契约的唯一依据。

命名约定：DTO 主键叫 product_id，ORM to_dict() 里叫 id（Service 层负责改名）。
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class ProductCreate(BaseModel):
    """POST /admin/products：管理员新建商品。status 不传默认上架 ON_SALE。"""

    name: str = Field(..., min_length=1, max_length=255)
    price: float = Field(..., gt=0, description="商品单价（元），必须为正")
    stock: int = Field(default=0, ge=0)
    description: str | None = None
    status: str = Field(default="ON_SALE", max_length=16, description="ON_SALE / OFF_SALE")


class ProductUpdate(BaseModel):
    """PATCH /admin/products/{id}：部分更新商品（只改传了的字段）。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    price: float | None = Field(default=None, gt=0)
    stock: int | None = Field(default=None, ge=0)
    description: str | None = None
    status: str | None = Field(default=None, max_length=16)


class ProductInfo(BaseModel):
    """商品信息 DTO（列表与详情共用的商品视图）。"""

    product_id: int
    name: str
    description: str | None = None
    price: float
    stock: int
    status: str
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ProductListResponse(BaseModel):
    """GET /products：商品分页列表出参。"""

    items: list[ProductInfo] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=0)
    offset: int = Field(..., ge=0)
