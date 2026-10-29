"""业务单号生成。

真实系统里单号通常要求"可读 + 唯一 + 不暴露自增 id"，这里采用：
    前缀 + yyyyMMddHHmmss + 4 位随机数
    ORDER20260925014233-4821
好处：能看出下单时间、便于客服口头核对、不用查库就能定位大致时间。
thread_id 用 uuid，因为它是 LangGraph 的上下文标识，不给人看。
"""

import random
import uuid
from datetime import datetime

from app.core.time import utcnow


def _stamp(moment: datetime | None = None) -> str:
    return (moment or utcnow()).strftime("%Y%m%d%H%M%S")


def _rand() -> str:
    return f"{random.randint(0, 9999):04d}"


def gen_order_no() -> str:
    return f"ORDER{_stamp()}{_rand()}"


def gen_payment_no() -> str:
    return f"PAY{_stamp()}{_rand()}"


def gen_refund_no() -> str:
    return f"REFUND{_stamp()}{_rand()}"


def gen_after_sale_no() -> str:
    return f"AS{_stamp()}{_rand()}"


def gen_ticket_no() -> str:
    return f"TK{_stamp()}{_rand()}"


def gen_tracking_no() -> str:
    return f"SF{_stamp()}{_rand()}"


def gen_thread_id(ticket_id: int) -> str:
    """LangGraph 执行上下文 id。

    一次**执行**一个 thread_id：用户开始新的一次处理时生成新的。
    客服批准后恢复同一次执行，必须复用原 thread_id 才能找到人工中断点。

    注意：同一次执行内部的**重试**不会再生成 thread_id，而是复用同一个 ——
    因为"从断点续跑"正是靠复用上下文实现的（见 agent_service._resume_decision）。
    """
    return f"ticket-{ticket_id}-{uuid.uuid4().hex[:12]}"


def gen_side_effect_key(kind: str, run_id: int | None, target: str) -> str:
    """副作用操作的幂等键（唯一约束保证同一件事只做一次）。

    组成：操作类型 + 执行标识 + 目标对象
        refund:run:12:order:345     → "第 12 次执行对订单 345 的退款"

    为什么要带 run_id：让"同一次执行内的重试/续跑"共用一把键（重复执行被唯一约束挡住），
    而用户重新发起一次处理（新的 AgentRun）是一个新的意图，应该允许再试一次 ——
    那时服务层的业务规则会重新判断该不该退。

    target 用稳定标识（order_id 这种业务主键），不要用时间戳之类每次都变的值，
    否则幂等键就失去意义了。
    """
    return f"{kind}:run:{run_id if run_id is not None else '-'}:{target}"
