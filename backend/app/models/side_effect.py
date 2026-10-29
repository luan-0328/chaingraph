"""副作用幂等记录（v2.md §60 的"重复请求"防护，落到数据库唯一约束上）。

解决什么问题
------------
"同一件事只做一次"目前主要靠业务规则（订单已退款 → 拒绝再次退款）。
那套规则是对的，但它依赖"每次都能正确读出最新状态"。真正兜底的是这里：
**一条带唯一约束的记录**。

    INSERT INTO side_effect_claims (dedup_key, ...) VALUES (...)
    -- 唯一约束 dedup_key：第二个想插入同样 key 的请求直接撞唯一键

于是即使前面的判断因为任何原因没拦住（并发、重试、断点续跑、代码改坏），
数据库也不会让同一个副作用落两次。

幂等键的粒度（core/ids.py 的 gen_side_effect_key）
--------------------------------------------------
    refund:run:12:order:345    "第 12 次 Agent 执行对订单 345 的退款"

    · 同一次执行内重试 / 断点续跑 → 同一把键 → 第二次被挡住（这正是我们要的）
    · 用户重新发起处理（新 AgentRun）→ 新键 → 允许再走一遍业务规则

和"退款状态检查"是两层防护，不重复：
    业务规则管**该不该做**（订单已退款、未支付、重复申请……）
    幂等键管**做没做过**（哪怕规则判断被绕过，副作用也不会重复落库）
"""

from sqlalchemy import BigInteger, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class SideEffectKind:
    """会被幂等键保护的操作类型。"""

    REFUND = "refund"
    PAYMENT = "payment"
    ORDER_CANCEL = "order_cancel"


class SideEffectClaim(Base, TimestampMixin):
    """一次副作用操作的"认领记录"。"""

    __tablename__ = "side_effect_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 幂等键（唯一）—— 同一个键只允许存在一条记录
    dedup_key: Mapped[str] = mapped_column(String(191), nullable=False)
    #: 发起这次操作的 AgentRun（人工直接操作时为 NULL）
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ticket_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 目标业务对象（订单号 / 退款单号……），便于排查
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 结果快照：重复请求可以拿到"上次做到哪了"，而不是只回一句冲突
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_side_effect_claims_dedup_key"),
        Index("idx_side_effect_claims_run_id", "agent_run_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "dedup_key": self.dedup_key,
            "agent_run_id": self.agent_run_id,
            "ticket_id": self.ticket_id,
            "target": self.target,
            "result": self.result,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:
        return f"<SideEffectClaim {self.kind} {self.dedup_key!r}>"
