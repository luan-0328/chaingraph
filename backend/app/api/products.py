"""商品接口（v2.md §7.3）。

普通用户：GET /products、GET /products/{id}（只看上架商品）
管理端接口在 admin.py（POST/PATCH/DELETE /admin/products）
"""

from fastapi import APIRouter, Query

from app.api import mappers
from app.core.logging import get_logger
from app.schemas.product import ProductInfo, ProductListResponse
from app.services import product_service

logger = get_logger(__name__)

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=ProductListResponse, summary="商品列表")
def list_products(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None, max_length=100),
) -> ProductListResponse:
    """商品列表（只返回上架商品；搜索按名称/描述模糊匹配）。"""
    result = product_service.list_products(
        limit=limit, offset=offset, keyword=keyword, only_on_sale=True
    )
    return ProductListResponse(
        items=[ProductInfo(**mappers.product_info(p)) for p in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/{product_id}", response_model=ProductInfo, summary="商品详情")
def get_product(product_id: int) -> ProductInfo:
    product = product_service.get_product(product_id, only_on_sale=True)
    return ProductInfo(**mappers.product_info(product))
