"""业务异常：一处定义，全局翻译成 HTTP 状态码。

设计要点：
    1. 每个异常自带 `code`（机器可读，会返回给前端和 LLM）与 `http_status`；
    2. Router / Service 只管 raise，不用写 try/except 翻译（见 main.py 的异常处理器）；
    3. Agent 的工具层把 BusinessError 转成 {"success": false, "error": CODE} 交回给模型，
       而不是把 Python 堆栈扔给 LLM。
"""


class BusinessError(Exception):
    """所有业务异常的基类。"""

    code = "BUSINESS_ERROR"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict | None = None,
    ) -> None:
        self.message = message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict:
        """给 Agent 工具返回用的结构。"""
        return {"success": False, "error": self.code, "message": self.message}


# ======================================================================
# 401 / 403
# ======================================================================
class AuthenticationError(BusinessError):
    """没登录 / token 无效或过期 → 401。

    前端拿到这个码应当清掉本地 token 并跳登录页（而不是弹一句"操作失败"）。
    """

    code = "UNAUTHORIZED"
    http_status = 401


class PermissionDeniedError(BusinessError):
    """越权访问他人资源，或角色不足。"""

    code = "FORBIDDEN"
    http_status = 403


# ======================================================================
# 404
# ======================================================================
# 下面这些"XX 不存在"的类看起来只是换个 code，但它们是**接口契约的一部分**：
# 前端和各验收脚本都按 code 分支（例如重试逻辑只对 DEPENDENCY_* 重试），
# 所以宁可多几个类，也不要到处复用一句 NOT_FOUND 让人去猜是什么不存在。
class NotFoundError(BusinessError):
    """资源不存在 → 404。父类，不要直接 raise 它。"""

    code = "NOT_FOUND"
    http_status = 404


class UserNotFoundError(NotFoundError):
    """用户不存在（登录/查他人资料时）。"""

    code = "USER_NOT_FOUND"


class ProductNotFoundError(NotFoundError):
    """商品不存在。"""

    code = "PRODUCT_NOT_FOUND"


class CartItemNotFoundError(NotFoundError):
    """购物车行不存在（可能已在别的页面被结算/删掉了）。"""

    code = "CART_ITEM_NOT_FOUND"


class OrderNotFoundError(NotFoundError):
    """订单不存在。Agent 工具把它转成结构化错误返回给模型，而不是抛穿。"""

    code = "ORDER_NOT_FOUND"


class PaymentNotFoundError(NotFoundError):
    """支付流水不存在。"""

    code = "PAYMENT_NOT_FOUND"


class LogisticsNotFoundError(NotFoundError):
    """物流单不存在（通常是"还没发货"，此时不该按错误处理而是如实说未发货）。"""

    code = "LOGISTICS_NOT_FOUND"


class AfterSaleNotFoundError(NotFoundError):
    """售后单不存在。"""

    code = "AFTER_SALE_NOT_FOUND"


class RefundNotFoundError(NotFoundError):
    """退款流水不存在。"""

    code = "REFUND_NOT_FOUND"


class TicketNotFoundError(NotFoundError):
    """工单不存在。"""

    code = "TICKET_NOT_FOUND"


class KnowledgeNotFoundError(NotFoundError):
    """知识文档不存在。"""

    code = "KNOWLEDGE_NOT_FOUND"


class AgentRunNotFoundError(NotFoundError):
    """Agent 执行记录不存在。"""

    code = "AGENT_RUN_NOT_FOUND"


# ======================================================================
# 409 冲突：状态非法 / 重复操作 / 库存不足
# ======================================================================
class ConflictError(BusinessError):
    """状态冲突 → 409。父类，不要直接 raise 它。

    409 在本项目里承载了**全部幂等语义**：重复支付、重复退款、
    并发抢占失败都走这个状态码 + 不同的 code，前端据此区分
    "你重复点了"（提示一下就好）和"别人正在处理"（让用户等等）。
    """

    code = "CONFLICT"
    http_status = 409


class InvalidStateError(ConflictError):
    """当前状态不允许该操作（例如未支付就想发货）。"""

    code = "INVALID_STATE"


class DuplicateOperationError(ConflictError):
    """幂等保护：重复支付、重复退款等。"""

    code = "DUPLICATE_OPERATION"


class InsufficientStockError(ConflictError):
    """库存不足（并发扣减时 `WHERE stock >= ?` 没抢到也会抛它）。"""

    code = "INSUFFICIENT_STOCK"


class ProductOffSaleError(ConflictError):
    """商品已下架，不能加购/下单。"""

    code = "PRODUCT_OFF_SALE"


# ======================================================================
# 400 参数/规则
# ======================================================================
class ValidationError(BusinessError):
    """请求参数或业务数值不合法（退款金额 <=0、超过订单金额等）→ 400。

    和 422（Pydantic 结构校验失败）的区别：422 是"字段类型/长度不对"，
    400 是"格式没问题但业务上说不通"。
    """

    code = "VALIDATION_ERROR"
    http_status = 400


class RefundNotAllowedError(BusinessError):
    """退款规则不允许（金额超限、已发货、状态不符等）。"""

    code = "REFUND_NOT_ALLOWED"
    http_status = 400


class EmptyCartError(ValidationError):
    """购物车为空就结算。"""

    code = "EMPTY_CART"


# ======================================================================
# Agent 相关
# ======================================================================
class AgentExecutionError(BusinessError):
    """Agent 执行失败（LLM 挂了、结构化输出连续为空等）→ 500。

    注意 code 是 AGENT_EXECUTION_FAILED，而 `is_retryable_error()` 里
    专门为它开了个口子：它可能只是"包了一层"的执行失败，真正该看的是
    底层原因是否可恢复（超时/429 才重试，业务错误不重试）。
    """

    code = "AGENT_EXECUTION_FAILED"
    http_status = 500


class AgentWaitingHumanError(BusinessError):
    """Agent 停在人工确认点：approve/reject 之外的重复触发。"""

    code = "AGENT_WAITING_HUMAN"
    http_status = 409


# ======================================================================
# 503 依赖不可用
# ======================================================================
class VectorStoreUnavailableError(BusinessError):
    """向量库写不进去（Qdrant 连不上 / 维度对不上 / 索引维护失败）→ 503。

    为什么单独一个码：它回答的是"发布为什么失败"。
    知识发布失败时状态**停在草稿**（不会留下"显示已发布、实际检索不到"的假发布），
    拿到这个码就说明该先去看向量库（/health 与知识库概览都能看到后端与点数），
    修好后重新发布或调 POST /api/admin/knowledge/reindex 即可。
    """

    code = "VECTOR_STORE_UNAVAILABLE"
    http_status = 503

