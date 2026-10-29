"""FastAPI 应用入口。

启动：
    cd D:\\chaingraph\\backend
    uv run uvicorn app.main:app --reload

启动时自动：建库 → 建表 → dev 环境灌演示数据（用户/商品/知识库）。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.after_sales import router as after_sales_router
from app.api.auth import router as auth_router
from app.api.cart import router as cart_router
from app.api.customer_service import router as customer_service_router
from app.api.orders import router as orders_router
from app.api.products import router as products_router
from app.api.tickets import router as tickets_router
from app.core.config import settings
from app.core.database import init_database
from app.core.exceptions import BusinessError
from app.core.logging import get_logger, new_request_id, request_id_var, setup_logging

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子。"""
    logger.info("启动 %s v%s（env=%s）", settings.app_name, settings.app_version, settings.app_env)

    if init_database():
        logger.info("MySQL 就绪 → %s", settings.database_name)
    else:
        logger.error("数据库未就绪：接口会报错，请检查 .env 里的 DATABASE_URL")

    # 图与 Checkpoint 仅由独立 worker 持有；API 只提交任务和读取数据库事件。
    logger.info("Agent 使用持久化任务，请单独运行 python -m app.worker")

    # ---- RAG：把 knowledge/*.md 同步进库（切分+向量化，幂等）----
    try:
        from app.rag.ingest import ingest_all, store_size

        if store_size() == 0:
            result = ingest_all()
            logger.info("知识库首次入库：文档 %s 篇 / 分块 %s 个", result["documents"], result["chunks"])
        else:
            logger.info("知识库已有向量：%s 块", store_size())
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库入库失败（RAG 检索会返回空）：%s", exc)

    # ---- 向量索引：确认索引与库一致（首次切到 Qdrant 时在这里自动补建，不重算向量）----
    try:
        from app.rag.index import ensure_index_ready

        index_state = ensure_index_ready()
        if index_state.get("in_sync"):
            logger.info("向量索引就绪：%s", index_state)
        else:
            logger.warning("向量索引未就绪（RAG 可能少召回）：%s", index_state)
        # 库里存在旧维度的分块（换过 embedding 模型但没重算向量）：这类分块检索不到，
        # 而且**不报错**——所以在这里主动说清楚"要做什么"，否则会被当成"RAG 坏了"
        skipped = index_state.get("skipped_dim") or {}
        if skipped:
            logger.warning(
                "存在 %s 个分块与当前向量维度不一致（%s）：用"
                " POST /api/admin/knowledge/ingest?force=true 重算向量后才会进向量索引",
                sum(skipped.values()),
                skipped,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量索引自检失败（RAG 可能少召回）：%s", exc)

    if settings.llm_configured:
        logger.info("LLM 就绪：model=%s base_url=%s", settings.llm_model, settings.llm_base_url)
    else:
        logger.warning("未配置 LLM_API_KEY：Agent 接口会失败（业务接口不受影响）")

    if settings.embedding_configured:
        logger.info("Embedding 就绪：model=%s", settings.embedding_model)
    else:
        logger.warning("未配置 EMBEDDING：RAG 使用本地降级向量器（词法相似度，语义效果弱）")

    yield

    # ---- 关闭 ----
    try:
        from app.memory.checkpoint import close_checkpointer

        await close_checkpointer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭 Checkpoint 出错：%s", exc)

    # 关闭向量库客户端：本地内嵌模式的目录锁靠它释放（否则下次启动/别的实例打不开）
    try:
        from app.rag.vector_store import reset_vector_store

        reset_vector_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭向量库出错：%s", exc)

    logger.info("应用已关闭")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="智能电商客服与售后工单平台 —— 商品/购物车/订单/支付/物流/售后/工单 + LangGraph Agent",
    lifespan=lifespan,
)


# ======================================================================
# 中间件
# ======================================================================
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """为每个请求生成 request_id，串起这一条请求的所有日志（v2.md §62）。"""
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)

    response.headers["X-Request-Id"] = request_id
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


# ======================================================================
# 统一异常处理（v2.md §62：不能什么都返回 500）
# ======================================================================
@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    """所有业务异常在这里翻译成合适的 HTTP 状态码。

    返回体同时带 `code`（机器可读）和 `message`（人话），
    `detail` 是为了兼容 FastAPI 的默认错误结构。
    """
    logger.info(
        "业务异常 [%s] %s %s → %s %s",
        exc.code,
        request.method,
        request.url.path,
        exc.http_status,
        exc.message,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.message,
            **(exc.details or {}),
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """把 FastAPI 自己的 HTTPException（401/403/404 等）也统一成 {code, message, detail}。

    这样前端只需要读 `message` 一个字段，不用区分"业务异常"和"框架异常"。
    """
    code_by_status = {
        status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
        status.HTTP_403_FORBIDDEN: "FORBIDDEN",
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_409_CONFLICT: "CONFLICT",
        status.HTTP_422_UNPROCESSABLE_ENTITY: "VALIDATION_ERROR",
    }
    message = exc.detail if isinstance(exc.detail, str) else "请求处理失败"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": code_by_status.get(exc.status_code, "HTTP_ERROR"),
            "message": message,
            "detail": message,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """参数校验失败 → 422，并把第一个错误翻译成人话。"""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", [])[1:]) or "参数"
    message = f"请求参数不合法：{field} {first.get('msg', '')}".strip()

    logger.info("参数校验失败 %s %s：%s", request.method, request.url.path, message)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": "VALIDATION_ERROR",
            "message": message,
            "detail": message,
            "errors": exc.errors()[:5],
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：未预料到的异常。**不把堆栈返回给前端**（v2.md §47），只写日志。"""
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": "INTERNAL_ERROR",
            "message": "服务器处理失败，请稍后重试",
            "detail": "服务器处理失败，请稍后重试",
        },
    )


# ======================================================================
# 路由
# ======================================================================
app.include_router(auth_router, prefix="/api")
app.include_router(products_router, prefix="/api")
app.include_router(cart_router, prefix="/api")
app.include_router(orders_router, prefix="/api")
app.include_router(after_sales_router, prefix="/api")
app.include_router(tickets_router, prefix="/api")
app.include_router(customer_service_router, prefix="/api")
app.include_router(admin_router, prefix="/api")


@app.get("/health", tags=["system"], summary="健康检查")
def health_check() -> dict:
    from app.rag.vector_store import active_backend_name

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "app_env": settings.app_env,
        "database": settings.database_name,
        "llm_configured": settings.llm_configured,
        "embedding_configured": settings.embedding_configured,
        # 向量库：配置值 + 进程里**实际生效**的实现（Qdrant 连不上时会降级成 mysql，
        # 这里要能不撒谎 —— 详细现状（点数/维度/是否同步）在 GET /api/admin/knowledge/stats）
        "vector_store": settings.vector_store,
        "vector_store_active": active_backend_name(),
        # 一眼看出这个实例跑的是真实模型还是并发测试桩
        "agent_stub_llm": settings.agent_stub_llm,
    }
