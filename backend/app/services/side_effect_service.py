"""幂等守护：让"会动钱/动货"的操作在数据库层面只做一次。

用法（Agent 的写操作必须包一层）：

    guard = SideEffectGuard(kind=SideEffectKind.REFUND, agent_run_id=run_id,
                            target=f"order:{order_id}")
    existing = guard.claim()          # 认领；返回 None=我是第一个
    if existing is not None:          # 已经做过 → 直接返回上次结果
        return existing.result
    try:
        payload = do_the_real_thing()
    except Exception:
        guard.release()               # 失败要放开，否则重试会一直被自己的记录挡住
        raise
    guard.complete(payload)           # 记住结果

关键设计
--------
1) **认领是并发安全的**：靠 `side_effect_claims.dedup_key` 的唯一约束。
   两个并发请求同时 INSERT，MySQL 只让一个成功，另一个拿到 1062 → 判定为重复。
   这比"先查再插"可靠（那中间有竞态窗口）。

2) **失败必须释放**：如果真实操作失败（LLM 超时、退款被业务规则拒绝），
   认领记录要删掉，否则"同一次执行"后续的重试会被自己的记录挡住，
   反而变成"一次失败就永远不能重试"。

3) **成功保留结果**：重复请求能拿到上次的结果快照（而不是只有一句冲突），
   前端/模型都能如实转述"这件事之前已经做过了，结果是……"。
"""

from sqlalchemy import delete, select
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import IntegrityError

from app.core.database import session_scope
from app.core.ids import gen_side_effect_key
from app.core.logging import get_logger
from app.models.side_effect import SideEffectClaim, SideEffectKind

logger = get_logger(__name__)


class SideEffectGuard:
    """一次副作用操作的幂等守护（对象只用于一次操作）。"""

    def __init__(
        self,
        *,
        kind: str,
        agent_run_id: int | None,
        target: str,
        ticket_id: int | None = None,
    ) -> None:
        self.kind = kind
        self.agent_run_id = agent_run_id
        self.target = target
        self.ticket_id = ticket_id
        self.dedup_key = gen_side_effect_key(kind, agent_run_id, target)
        self._claimed = False

    # ------------------------------------------------------------------
    # 认领
    # ------------------------------------------------------------------
    def claim(self) -> dict | None:
        """尝试认领。

        返回 None  → 认领成功，调用方去执行真正的操作；
        返回 dict  → 已经有同样的操作做过，dict 是上次的记录（含 result 快照）。
        """
        try:
            with session_scope() as db:
                claim = SideEffectClaim(
                    kind=self.kind,
                    dedup_key=self.dedup_key,
                    agent_run_id=self.agent_run_id,
                    ticket_id=self.ticket_id,
                    target=self.target,
                )
                db.add(claim)
                db.flush()
                self._claimed = True
                logger.info("副作用认领成功：%s", self.dedup_key)
                return None
        except IntegrityError:
            # 唯一键冲突 = 已经有人认领过同一件事
            existing = self._find()
            logger.warning(
                "副作用重复请求被幂等键挡住：%s（上次记录 id=%s）",
                self.dedup_key,
                (existing or {}).get("id"),
            )
            return existing or {"dedup_key": self.dedup_key, "result": None}

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def complete(self, result: dict | None) -> None:
        """记录结果快照（重复请求可以据此如实转述）。"""
        if not self._claimed:
            return
        try:
            with session_scope() as db:
                row = db.scalar(
                    select(SideEffectClaim).where(SideEffectClaim.dedup_key == self.dedup_key)
                )
                if row is not None:
                    row.result = jsonable_encoder(result)
                    db.flush()
        except Exception as exc:  # noqa: BLE001 —— 结果快照写失败不影响业务
            logger.warning("写入副作用结果失败（不影响业务）：%s", exc)

    def release(self) -> None:
        """真实操作失败 → 放开认领，让同一次执行的重试还有机会。"""
        if not self._claimed:
            return
        try:
            with session_scope() as db:
                db.execute(
                    delete(SideEffectClaim).where(SideEffectClaim.dedup_key == self.dedup_key)
                )
            self._claimed = False
            logger.info("副作用认领已释放（操作失败，允许重试）：%s", self.dedup_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("释放副作用认领失败：%s", exc)

    # ------------------------------------------------------------------
    def _find(self) -> dict | None:
        """按幂等键回查已有记录（撞唯一键之后用它拿到"上次做到哪了"）。

        查不到不算错误：极端情况下（另一请求刚 INSERT 又立刻 release）
        记录可能已经不在了，这时返回 None，由调用方当成"没有历史结果"处理。
        整个方法吞掉异常 —— 它只在"已经判定为重复"的分支里被调用，
        不该因为回查失败把一条本该幂等返回的请求变成 500。
        """
        try:
            with session_scope() as db:
                row = db.scalar(
                    select(SideEffectClaim).where(SideEffectClaim.dedup_key == self.dedup_key)
                )
                return row.to_dict() if row is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询副作用记录失败：%s", exc)
            return None


__all__ = ["SideEffectGuard", "SideEffectKind"]
