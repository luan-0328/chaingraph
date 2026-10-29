"""退款工具 —— 唯一能真正"动钱"的业务工具。

v2.md §37 / §70 的要求在这里落地：
    Agent 判断"该退款" ≠ 系统就退款。
    apply_refund 只是把请求交给 RefundService，由它重新校验：
        订单是否存在 / 是否本人 / 是否已支付 / 是否已发货 / 是否重复退款 / 金额是否超限
    校验不通过就返回结构化错误或"需要人工"，模型无法绕过。
"""

from langchain_core.tools import tool

from app.core.exceptions import BusinessError
from app.core.logging import get_logger
from app.services import refund_service

logger = get_logger(__name__)


@tool
def apply_refund(
    order_id: int,
    user_id: int,
    amount: float | None = None,
    reason: str = "",
    approved_by: int | None = None,
) -> dict:
    """对订单发起退款（高风险操作，后端会重新做完整业务校验与幂等检查）。

    什么时候用：用户明确要求退款，且你已确认订单事实（用 query_order 查过）。

    返回三种情况，都要如实转述给用户：
        ① {"success": true, "executed": true,  "refund": {...}}          退款已成功
        ② {"success": true, "executed": false, "need_human": true, ...}  需要人工审核（已发货/金额超限）
        ③ {"success": false, "error": "DUPLICATE_REFUND" / ...}          规则不允许（重复退款、状态不符等）

    参数：
        order_id:    订单 ID
        user_id:     当前用户 ID（归属校验）
        amount:      退款金额，不传 = 订单全额
        reason:      退款原因
        approved_by: 客服审批人工（Agent 自动调用时不要传）
    """
    try:
        result = refund_service.apply_refund(
            order_id=order_id,
            user_id=user_id,
            amount=amount,
            reason=reason or None,
            approved_by=approved_by,
        )
    except BusinessError as exc:
        logger.info("工具 apply_refund 业务失败：%s %s", exc.code, exc.message)
        return exc.to_dict()

    return {
        "success": True,
        "executed": result["executed"],
        "need_human": result.get("need_human", False),
        "reason": result.get("reason"),
        "refund": result.get("refund"),
        "order": {
            "order_no": result["order"]["order_no"],
            "payment_status": result["order"]["payment_status"],
            "after_sale_status": result["order"]["after_sale_status"],
        },
    }


@tool
def check_refund_eligibility(order_id: int, user_id: int, amount: float | None = None) -> dict:
    """只判断"这笔退款能不能办、要不要人工"，**不执行任何写操作**。

    什么时候用：用户问"我这个能退吗"，或者你想在承诺之前先确认规则。
    """
    try:
        evaluation = refund_service.evaluate_refund(
            order_id=order_id, user_id=user_id, amount=amount
        )
    except BusinessError as exc:
        return exc.to_dict()

    return {
        "success": True,
        "data": {
            "allowed": True,
            "need_human": evaluation["need_human"],
            "amount": evaluation["amount"],
            "reason": evaluation["reason"],
            "order_status": {
                "payment_status": evaluation["order"]["payment_status"],
                "fulfillment_status": evaluation["order"]["fulfillment_status"],
                "after_sale_status": evaluation["order"]["after_sale_status"],
            },
        },
    }
