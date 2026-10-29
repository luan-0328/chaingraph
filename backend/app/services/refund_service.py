"""退款服务 —— 全项目业务规则最密集的地方（v2.md §17、§37、§38、§60）。

一条铁律（v2.md §70）：
    "Agent 说这个订单应该退款" ≠ 数据库就应该退款。
    真正执行必须经过本服务的重新校验。

规则（v2.md §17）：
    1. 订单存在
    2. 当前用户拥有该订单
    3. 订单已经支付
    4. 订单尚未发货（已发货 → 转人工，不能自动退）
    5. 当前没有进行中的退款（幂等）
    6. 退款金额符合自动处理范围（<= REFUND_AUTO_APPROVE_LIMIT，否则转人工）

对外三个入口：
    evaluate_refund()          只评估，不落库（Agent 用它判断该不该调 apply_refund）
    apply_refund()             Agent / 系统尝试退款：能自动就自动，需人工就返回 need_human
    execute_approved_refund()  客服批准后真正执行（HITL 的 resume 路径）
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import (
    DuplicateOperationError,
    InvalidStateError,
    OrderNotFoundError,
    PermissionDeniedError,
    RefundNotAllowedError,
    ValidationError,
)
from app.core.ids import gen_refund_no
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.after_sale import AfterSaleStatus, AfterSaleType
from app.models.order import FulfillmentStatus, OrderAfterSaleStatus, PaymentStatus
from app.models.refund import RefundStatus
from app.models.user import Role, UserStatus
from app.repositories import (
    after_sale_repository,
    order_repository,
    product_repository,
    refund_repository,
    user_repository,
)
from app.services import after_sale_service

logger = get_logger(__name__)

# 这些履约状态表示"货还在商家手里"，可以自动退款
NOT_SHIPPED_STATUSES = (FulfillmentStatus.PENDING, FulfillmentStatus.PROCESSING)


# ======================================================================
# 规则评估（不落库）
# ======================================================================
def evaluate_refund(*, order_id: int, user_id: int, amount: Decimal | float | None = None) -> dict:
    """评估这笔退款能不能办、要不要人工。

    硬性不满足 → 抛业务异常；
    规则上"能做但需要人工" → 返回 need_human=True，由调用方决定（Agent 转 HITL）。
    """
    with session_scope() as db:
        order = order_repository.get_by_id(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if order["user_id"] != user_id:
            raise PermissionDeniedError("无权对他人的订单申请退款")

        existing_pending = refund_repository.find_pending_by_order(db, order_id)

    reasons: list[str] = []

    # ③ 已支付
    if order["payment_status"] == PaymentStatus.REFUNDED:
        raise DuplicateOperationError("该订单已经退款完成", code="ALREADY_REFUNDED")
    if order["payment_status"] != PaymentStatus.PAID:
        raise RefundNotAllowedError("订单未支付，无需退款")

    # ⑤ 幂等：不能有进行中的退款
    if existing_pending is not None:
        raise DuplicateOperationError(
            f"该订单已有进行中的退款（{existing_pending['refund_no']}）",
            code="DUPLICATE_REFUND",
        )

    # ⑥ 金额
    final_amount = _resolve_amount(order, amount)

    # ④ 是否已发货
    need_human = False
    if order["fulfillment_status"] not in NOT_SHIPPED_STATUSES:
        need_human = True
        reasons.append(
            f"订单当前履约状态为 {order['fulfillment_status']}（已发货/已签收），需人工审核"
        )

    # ⑥ 金额是否超过自动处理范围
    if final_amount > Decimal(str(settings.refund_auto_approve_limit)):
        need_human = True
        reasons.append(
            f"退款金额 {final_amount} 超过自动处理上限 {settings.refund_auto_approve_limit} 元，需人工审核"
        )

    if not reasons:
        reasons.append("订单已支付且未发货，金额在自动处理范围内，可自动退款")

    return {
        "order": order,
        "amount": float(final_amount),
        "need_human": need_human,
        "reason": "；".join(reasons),
    }


# ======================================================================
# 执行退款
# ======================================================================
def apply_refund(
    *,
    order_id: int,
    user_id: int,
    amount: Decimal | float | None = None,
    reason: str | None = None,
    after_sale_id: int | None = None,
    approved_by: int | None = None,
) -> dict:
    """尝试退款。

    返回：
        {"executed": True,  "need_human": False, "refund": {...}, "order": {...}, "reason": "..."}
        {"executed": False, "need_human": True,  "order": {...}, "reason": "..."}   ← 需要人工

    注意：**需要人工时不会创建退款记录**，退款要等客服批准后由
    execute_approved_refund() 真正落库（否则会出现"钱没退但账上有退款单"）。
    """
    evaluation = evaluate_refund(order_id=order_id, user_id=user_id, amount=amount)

    if evaluation["need_human"] and approved_by is None:
        logger.info(
            "退款转人工：order_id=%s 原因=%s", order_id, evaluation["reason"]
        )
        return {
            "executed": False,
            "need_human": True,
            "order": evaluation["order"],
            "amount": evaluation["amount"],
            "reason": evaluation["reason"],
            "refund": None,
        }

    result = _execute(
        order_id=order_id,
        user_id=user_id,
        amount=evaluation["amount"],
        reason=reason or evaluation["reason"],
        after_sale_id=after_sale_id,
        approved_by=approved_by,
    )
    return {
        "executed": True,
        "need_human": False,
        "reason": evaluation["reason"],
        **result,
    }


def execute_approved_refund(
    *, order_id: int, operator_id: int, comment: str | None = None
) -> dict:
    """客服批准后执行退款（HITL 恢复路径）。

    与 apply_refund 的区别：**跳过 need_human 判断**（人工已经批准了），
    但其余规则（已支付、未重复退款、金额合法）仍然重新校验一遍 —— 人工批准也不能违反业务规则。
    """
    with session_scope() as db:
        order = order_repository.get_by_id(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")

        existing_pending = refund_repository.find_pending_by_order(db, order_id)

    if order["payment_status"] == PaymentStatus.REFUNDED:
        raise DuplicateOperationError("该订单已经退款完成", code="ALREADY_REFUNDED")
    if order["payment_status"] != PaymentStatus.PAID:
        raise RefundNotAllowedError("订单未支付，无法退款")
    if existing_pending is not None:
        raise DuplicateOperationError(
            f"该订单已有进行中的退款（{existing_pending['refund_no']}）", code="DUPLICATE_REFUND"
        )

    amount = _resolve_amount(order, None)
    result = _execute(
        order_id=order_id,
        user_id=order["user_id"],
        amount=float(amount),
        reason=comment or "客服人工审核通过，执行退款",
        after_sale_id=None,
        approved_by=operator_id,
    )

    logger.info(
        "人工批准退款：order_no=%s 金额=%s operator=%s",
        order["order_no"],
        amount,
        operator_id,
    )
    return result


def reject_refund(*, order_id: int, operator_id: int, reason: str | None = None) -> dict:
    """客服拒绝退款：售后单置 REJECTED，订单售后状态回退。"""
    with session_scope() as db:
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")

        _validate_operator(db, operator_id)
        after_sale = after_sale_repository.find_active_by_order(db, order_id)
        if after_sale is None:
            raise InvalidStateError("该订单没有进行中的售后，无需拒绝")

        record = after_sale_repository.get_model(db, after_sale["id"])
        if record is not None:
            record.status = AfterSaleStatus.REJECTED
            if reason:
                record.reason = f"{record.reason or ''}｜拒绝原因：{reason}"
            db.flush()
            db.refresh(record)
            after_sale_dict = record.to_dict()
        else:
            after_sale_dict = after_sale

        order.after_sale_status = OrderAfterSaleStatus.NONE
        db.flush()
        db.refresh(order)
        order_dict = order.to_dict()

    logger.info("人工拒绝退款：order_no=%s operator=%s", order_dict["order_no"], operator_id)
    return {"order": order_dict, "after_sale": after_sale_dict}


# ======================================================================
# 查询
# ======================================================================
def list_refunds(
    *, limit: int = 20, offset: int = 0, status: str | None = None
) -> dict:
    """客服/管理端退款流水列表（可按状态筛），带分页信息。"""
    with session_scope() as db:
        items, total = refund_repository.list_all(
            db, limit=limit, offset=offset, status=status
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def list_refunds_by_order(order_id: int, *, user_id: int | None = None) -> list[dict]:
    """某订单的退款流水。

    user_id 不为 None 时校验归属（普通用户只能看自己订单的退款记录）；
    客服/管理端传 None（他们本来就要看全量）。
    """
    with session_scope() as db:
        order = order_repository.get_by_id(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")
        if user_id is not None and order["user_id"] != user_id:
            raise PermissionDeniedError("无权查看他人订单的退款记录")
        return refund_repository.list_by_order(db, order_id)


def _claim_refund_or_die(db, refund_id: int) -> dict:  # noqa: ANN001
    """用条件更新把这一笔退款"占下来"；抢不到就抛业务异常。

    为什么退款成功与否要以这条 UPDATE 的 affected_rows 为准（v2.md 的重复退款防护）：

        UPDATE refund_records SET status='SUCCESS', completed_at=?
         WHERE id=? AND status='PENDING'          -- ← 只有 1 行能被改到

    前面的 `evaluate_refund` / 事务内复查都是"读"，读到的状态随时可能被
    另一个并发事务改掉；而这一条是"检查 + 修改"在同一条 SQL 里完成的，
    数据库保证只有一个请求拿到那一行。抢到的人才继续改订单 / 售后单 / 库存；
    抢不到的直接抛错 → 整个事务回滚 → 不会出现第二笔退款流水。

    这是三层防护里的第 ① 层：
        ① 本方法（条件更新）
        ② refund_records.active_order_id 唯一索引（数据库兜底）
        ③ side_effect_claims 幂等键（同一次 Agent 执行只做一次）
    """
    refund = refund_repository.claim_success(db, refund_id, utcnow())
    if refund is None:
        raise DuplicateOperationError(
            "该退款已经处理过，本次未重复执行", code="DUPLICATE_REFUND"
        )
    return refund


# ======================================================================
# 内部：真正落库的退款执行（一个事务）
# ======================================================================
def _execute(
    *,
    order_id: int,
    user_id: int,
    amount: float,
    reason: str,
    after_sale_id: int | None,
    approved_by: int | None,
) -> dict:
    """退款落库：改退款流水 + 售后单 + 订单，必要时回补库存。全在一个事务里。"""
    with session_scope() as db:
        order = order_repository.get_model_for_update(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"订单 {order_id} 不存在")

        if order.user_id != user_id:
            raise PermissionDeniedError("无权对他人的订单申请退款")
        amount = float(_resolve_amount(order.to_dict(), amount))
        if approved_by is not None:
            _validate_operator(db, approved_by)
            from app.core.execution import execution_claim
            claim = execution_claim.get()
            if claim is not None:
                from app.models.agent_run import AgentRun
                from app.services.agent_task_service import order_review_snapshot
                run = db.get(AgentRun, claim.run_id)
                expected = (run.review_payload or {}).get("order_snapshot")
                if expected != order_review_snapshot(db, order_id):
                    return {"executed": False, "need_human": True, "refund": None,
                            "order": order.to_dict(), "amount": amount,
                            "reason": "审批后订单或售后状态发生变化，需重新审核"}

        # 事务内再查一次（防并发：两个请求同时走到这里）。
        # 注意这里的判断必须"重"一点：拿到了行锁说明前一个事务已提交，
        # 所以要按已提交的最终状态判断，而不是只看 PENDING
        # （本项目的退款是同步即时到账，前一笔提交时已经是 SUCCESS）。
        if order.payment_status == PaymentStatus.REFUNDED:
            raise DuplicateOperationError("该订单已经退款完成", code="ALREADY_REFUNDED")
        if order.payment_status != PaymentStatus.PAID:
            raise RefundNotAllowedError("订单未支付，无法退款")

        pending = refund_repository.find_pending_by_order(db, order_id)
        if pending is not None:
            raise DuplicateOperationError(
                f"该订单已有进行中的退款（{pending['refund_no']}）", code="DUPLICATE_REFUND"
            )

        refunded = refund_repository.find_success_by_order(db, order_id)
        if refunded is not None:
            raise DuplicateOperationError(
                f"该订单已经退款成功（{refunded['refund_no']}）", code="ALREADY_REFUNDED"
            )

        if approved_by is None and (
            order.fulfillment_status not in NOT_SHIPPED_STATUSES
            or Decimal(str(amount)) > Decimal(str(settings.refund_auto_approve_limit))
        ):
            # 评估后订单可能已经发货；最终规则必须在持有订单锁时检查。
            return {"executed": False, "need_human": True, "refund": None,
                    "order": order.to_dict(), "amount": amount,
                    "reason": "订单状态或退款金额需要人工审核，退款尚未执行"}

        # ---- 售后单：没有就补一张（Agent 自动退款场景用户可能只提了工单）----
        if after_sale_id is not None:
            sale = after_sale_repository.get_by_id(db, after_sale_id)
            if sale is None:
                raise InvalidStateError("关联的售后单不存在")
        else:
            sale = after_sale_repository.find_active_by_order(db, order_id)

        if sale is not None:
            if sale["order_id"] != order_id or sale["user_id"] != user_id:
                raise PermissionDeniedError("售后单与退款订单不匹配")
            if sale["type"] not in (AfterSaleType.REFUND, AfterSaleType.RETURN):
                raise InvalidStateError("该售后类型不能执行退款")
            if sale["status"] not in (AfterSaleStatus.APPLYING, AfterSaleStatus.PROCESSING,
                                      AfterSaleStatus.APPROVED):
                raise InvalidStateError("该售后单已结束，不能执行退款")
            _resolve_amount(order.to_dict(), sale["amount"])

        if sale is None:
            sale = after_sale_repository.create(
                db,
                after_sale_no=f"AS{gen_refund_no()[6:]}",
                order_id=order_id,
                user_id=user_id,
                type=AfterSaleType.REFUND,
                amount=amount,
                reason=reason,
                status=AfterSaleStatus.PROCESSING,
            )

        # ---- 退款流水：PENDING → SUCCESS（模拟即时到账）----
        # 关键：用**条件更新**抢这一笔退款（UPDATE ... WHERE status='PENDING'）。
        # 只有真的把 PENDING 改成 SUCCESS 的那个请求，才允许继续往下改
        # 售后单 / 订单 / 库存 —— 见 _claim_refund_or_die 的说明。
        refund = refund_repository.create(
            db,
            refund_no=gen_refund_no(),
            after_sale_id=sale["id"],
            order_id=order_id,
            user_id=user_id,
            amount=amount,
            reason=reason,
            status=RefundStatus.PENDING,
            approved_by=approved_by,
        )
        refund = _claim_refund_or_die(db, refund["id"])

        # ---- 售后单完成 ----
        sale = after_sale_repository.update_status(
            db, sale["id"], AfterSaleStatus.COMPLETED, completed_at=utcnow()
        )

        # ---- 订单：退款完成 ----
        # 同样用条件更新：只有"当前还是 PAID"的订单才允许被改成 REFUNDED。
        # 拿不到行说明订单已经被别人退掉了，直接抛错让整个事务回滚（连带上面刚抢到的
        # 退款流水也一起回滚）—— 宁可整笔不成立，也不要留一条"退了但订单没退"的脏数据。
        if not order_repository.claim_refunded(db, order_id, refunded_at=utcnow()):
            raise DuplicateOperationError(
                "订单状态已被其他退款请求变更，本次退款未执行", code="ALREADY_REFUNDED"
            )
        db.expire(order)
        db.refresh(order)

        # ---- 未发货退款 → 回补库存（货没出去，库存要还回来）----
        if order.fulfillment_status in NOT_SHIPPED_STATUSES:
            for item in order_repository.list_by_order(db, order_id):
                product_repository.increase_stock(db, item["product_id"], item["quantity"])

        order_dict = order.to_dict()

    logger.info(
        "退款成功：order_no=%s refund_no=%s 金额=%s approved_by=%s",
        order_dict["order_no"],
        refund["refund_no"],
        refund["amount"],
        approved_by,
    )
    return {"refund": refund, "after_sale": sale, "order": order_dict}


def _validate_operator(db, operator_id: int) -> None:
    operator = user_repository.get_by_id(db, operator_id)
    if (operator is None or operator["status"] != UserStatus.ACTIVE
            or operator["role"] not in Role.STAFF):
        raise PermissionDeniedError("退款审批人必须是有效的客服或管理员")


def _resolve_amount(order: dict, amount: Decimal | float | None) -> Decimal:
    """定下这次退多少钱，并做金额护栏。

    规则：
        amount 为 None → 订单全额（默认）
        amount 等于订单金额 → 全额退款（当前订单状态模型不支持部分退款）
        amount <= 0    → 拒绝
        amount > 订单金额 → 拒绝（**模型说"退 1000 元"就死在这里**，v2.md §70）

    全程用 Decimal 而不是 float：钱不能用二进制浮点算，
    `float(0.1) + float(0.2) != 0.3` 这种误差累积到对账时就是事故。
    """
    total = Decimal(str(order["total_amount"]))
    try:
        final = total if amount is None else Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("退款金额格式无效") from exc

    if not final.is_finite():
        raise ValidationError("退款金额必须是有限数值")
    if final <= 0:
        raise ValidationError("退款金额必须大于 0")
    if final > total:
        raise ValidationError(f"退款金额 {final} 超过订单金额 {total}")
    if final != total:
        raise ValidationError("当前仅支持订单全额退款，部分退款需要另行处理", code="PARTIAL_REFUND_UNSUPPORTED")
    return final
