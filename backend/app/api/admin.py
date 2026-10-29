"""管理端接口（v2.md §52）：商品管理、知识库管理、用户/订单/工单总览。

只允许 ADMIN 角色访问。注意 v2.md §58：权限不能只靠前端隐藏，后端必须再验一次。
"""

from fastapi import APIRouter, Depends, Query, status

from app.api import mappers
from app.core.dependencies import require_roles
from app.core.logging import get_logger
from app.models.user import Role
from app.repositories import user_repository
from app.schemas.admin import AdminUserInfo, AdminUserListResponse
from app.schemas.knowledge import (
    KnowledgeCreate,
    KnowledgeInfo,
    KnowledgeIngestResponse,
    KnowledgeListResponse,
    KnowledgeUpdate,
)
from app.schemas.product import ProductCreate, ProductInfo, ProductListResponse, ProductUpdate
from app.services import knowledge_service, order_service, product_service, ticket_service

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

admin_only = require_roles(Role.ADMIN)


# ======================================================================
# 商品管理
# ======================================================================
@router.get("/products", response_model=ProductListResponse, summary="商品列表（含下架）")
def list_products(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: dict = Depends(admin_only),
) -> ProductListResponse:
    result = product_service.list_products(
        limit=limit, offset=offset, keyword=keyword, status=status_filter
    )
    return ProductListResponse(
        items=[ProductInfo(**mappers.product_info(p)) for p in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.post(
    "/products", response_model=ProductInfo, status_code=status.HTTP_201_CREATED, summary="创建商品"
)
def create_product(
    payload: ProductCreate, current_user: dict = Depends(admin_only)
) -> ProductInfo:
    product = product_service.create_product(
        name=payload.name,
        price=payload.price,
        stock=payload.stock,
        description=payload.description,
        status=payload.status or "ON_SALE",
    )
    return ProductInfo(**mappers.product_info(product))


@router.patch("/products/{product_id}", response_model=ProductInfo, summary="修改商品")
def update_product(
    product_id: int,
    payload: ProductUpdate,
    current_user: dict = Depends(admin_only),
) -> ProductInfo:
    """只更新传了的字段（PATCH 语义）。"""
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    product = product_service.update_product(product_id, **fields)
    return ProductInfo(**mappers.product_info(product))


@router.delete(
    "/products/{product_id}", response_model=ProductInfo, summary="下架商品"
)
def delete_product(product_id: int, current_user: dict = Depends(admin_only)) -> ProductInfo:
    """软下架：历史订单明细要能追溯到商品，不做物理删除。"""
    product = product_service.take_off_sale(product_id)
    return ProductInfo(**mappers.product_info(product))


# ======================================================================
# 知识库管理（增删改查 + 发布/下线）
# ======================================================================
@router.get("/knowledge", response_model=KnowledgeListResponse, summary="知识文档列表")
def list_knowledge(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    category: str | None = Query(default=None, max_length=64),
    keyword: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    current_user: dict = Depends(admin_only),
) -> KnowledgeListResponse:
    result = knowledge_service.list_documents(
        limit=limit,
        offset=offset,
        category=category,
        keyword=keyword,
        status=status_filter,
    )
    return KnowledgeListResponse(
        items=[KnowledgeInfo(**mappers.knowledge_info(d)) for d in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/knowledge/stats", summary="知识库概览（草稿/已发布/已下线、向量块数）")
def knowledge_stats(current_user: dict = Depends(admin_only)) -> dict:
    """一眼看清"哪些知识在生效、哪些还没发布"。"""
    return knowledge_service.stats()


@router.get("/knowledge/search", summary="知识检索调试")
def search_knowledge(
    query: str = Query(..., min_length=1, max_length=200),
    top_k: int = Query(default=4, ge=1, le=20),
    current_user: dict = Depends(admin_only),
) -> dict:
    """直接调检索器看命中了什么（调知识库时非常有用）。

    顺序很重要：这条路由必须**排在 `/knowledge/{document_id}` 之前**，
    否则 FastAPI 会先匹配到带路径参数的那条，把 "search" 当成 document_id 去解析，
    结果是 422「Input should be a valid integer」—— 一个纯路由顺序造成的假故障。
    """
    return {"query": query, "hits": knowledge_service.search(query, top_k=top_k)}


@router.get("/knowledge/{document_id}", response_model=KnowledgeInfo, summary="知识文档详情")
def get_knowledge(
    document_id: int, current_user: dict = Depends(admin_only)
) -> KnowledgeInfo:
    return KnowledgeInfo(**mappers.knowledge_info(knowledge_service.get_document(document_id)))


@router.post(
    "/knowledge",
    response_model=KnowledgeInfo,
    status_code=status.HTTP_201_CREATED,
    summary="新增知识文档",
)
def create_knowledge(
    payload: KnowledgeCreate, current_user: dict = Depends(admin_only)
) -> KnowledgeInfo:
    """新增知识文档。

    默认存成**草稿**（`status=DRAFT`）：新知识要显式"发布"才进向量库、才能被 Agent 检索到
    （`publish=true` 可以在创建时就顺手发布）。
    """
    document = knowledge_service.create_document(
        title=payload.title,
        content=payload.content,
        category=payload.category,
        source=payload.source,
        publish=bool(payload.publish),
    )
    return KnowledgeInfo(**mappers.knowledge_info(document))


@router.patch("/knowledge/{document_id}", response_model=KnowledgeInfo, summary="修改知识文档")
def update_knowledge(
    document_id: int,
    payload: KnowledgeUpdate,
    current_user: dict = Depends(admin_only),
) -> KnowledgeInfo:
    """修改知识文档。

    **改了 content 会自动退回草稿**并作废旧向量 —— 必须重新发布一次，
    否则检索会用旧向量命中过时知识。只改标题/分类不影响状态。
    """
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    document = knowledge_service.update_document(document_id=document_id, **fields)
    return KnowledgeInfo(**mappers.knowledge_info(document))


@router.delete(
    "/knowledge/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除知识文档"
)
def delete_knowledge(document_id: int, current_user: dict = Depends(admin_only)) -> None:
    """删除文档，**连带它的分块与向量**（否则向量库里会留下永远被检索到的孤儿）。"""
    knowledge_service.delete_document(document_id)


@router.post(
    "/knowledge/{document_id}/publish",
    response_model=KnowledgeInfo,
    summary="发布知识（切分 → Embedding → 写入向量库）",
)
def publish_knowledge(
    document_id: int, current_user: dict = Depends(admin_only)
) -> KnowledgeInfo:
    """发布一篇知识，发布后 Agent 就能通过 RAG 检索到它。

    执行顺序（失败也不会留下"假发布"）：
        切分正文 → 算 Embedding → 写入向量库 → **全部成功之后**才把状态置为已发布。
    向量化失败时状态停在草稿，并把错误原因返回给管理员。
    """
    document = knowledge_service.publish_document(document_id)
    return KnowledgeInfo(**mappers.knowledge_info(document))


@router.post(
    "/knowledge/{document_id}/offline", response_model=KnowledgeInfo, summary="下线知识"
)
def offline_knowledge(
    document_id: int, current_user: dict = Depends(admin_only)
) -> KnowledgeInfo:
    """下线一篇知识：Agent 立刻检索不到它（分块与向量保留，重新发布不用重算）。"""
    document = knowledge_service.offline_document(document_id)
    return KnowledgeInfo(**mappers.knowledge_info(document))


@router.post("/knowledge/publish-drafts", summary="一键发布全部草稿")
def publish_drafts(current_user: dict = Depends(admin_only)) -> dict:
    """批量导入知识后一次性发布（返回发布篇数、生成块数、失败明细）。"""
    return knowledge_service.publish_all_drafts()


@router.post("/knowledge/ingest", response_model=KnowledgeIngestResponse, summary="知识入库（切分+向量化）")
def ingest_knowledge(
    force: bool = Query(default=False, description="强制重建全部向量（换 embedding 模型后用）"),
    current_user: dict = Depends(admin_only),
) -> KnowledgeIngestResponse:
    """把 knowledge/*.md 同步进库并做切分 + 向量化（RAG 的数据准备）。

    幂等：已有分块的文档会跳过；`force=true` 才重建全部向量。
    注意它**只处理已发布的文档**，不会把草稿悄悄上线（发布请用 publish 接口）。
    """
    from app.rag.ingest import ingest_all

    result = ingest_all(force=force)
    return KnowledgeIngestResponse(
        documents=result["documents"],
        chunks=result["chunks"],
    )


@router.post("/knowledge/reindex", summary="重建向量索引（只搬向量，不重新调用 Embedding）")
def reindex_knowledge(current_user: dict = Depends(admin_only)) -> dict:
    """把 knowledge_chunks 里的分块与向量**整体重灌**进向量库（Qdrant）。

    和 `/knowledge/ingest` 的区别是钱与时间：
        ingest?force=true   重新切分 + **重新调 Embedding**（慢、要花钱，换模型时才需要）
        reindex             只把库里现成的向量搬到向量索引（秒级，不花钱）

    什么时候需要它：索引被清空 / 换了向量库 / 知识库概览里
    `vector_store.in_sync=false`（点数与分块数对不上）。
    它不改文档状态，所以不会把草稿"顺手发布"出去。
    """
    return knowledge_service.reindex()


# ======================================================================
# 全量数据查看
# ======================================================================
@router.get("/orders", summary="订单总览")
def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(admin_only),
) -> dict:
    return order_service.list_all_orders(limit=limit, offset=offset)


@router.get("/tickets", summary="工单总览")
def list_tickets(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(admin_only),
) -> dict:
    return ticket_service.list_tickets_for_staff(limit=limit, offset=offset)


@router.get("/users", response_model=AdminUserListResponse, summary="用户总览")
def list_users(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = Query(default=None, max_length=64),
    role: str | None = Query(default=None),
    current_user: dict = Depends(admin_only),
) -> AdminUserListResponse:
    with session_scope() as db:
        items, total = user_repository.list_users(
            db, limit=limit, offset=offset, keyword=keyword, role=role
        )
    return AdminUserListResponse(
        items=[AdminUserInfo(**mappers.admin_user_info(u)) for u in items],
        total=total,
        limit=limit,
        offset=offset,
    )
