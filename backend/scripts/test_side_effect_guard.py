"""副作用幂等键的行为验证（直接打数据库，不需要后端）。

    cd backend
    uv run python scripts/test_side_effect_guard.py

验证四件事（这四条如果坏了，后果都是"重复动钱"）：
    ① 第一次认领成功
    ② 同一把键第二次认领被挡（唯一约束），且能拿到上次的结果快照
    ③ 真操作失败 → 释放认领 → 同一次执行重试还能再做
    ④ 换一次执行（新 agent_run_id）→ 新键 → 允许再走一遍业务规则

注意：唯一约束是并发安全的根本 —— 两个并发请求同时 INSERT，MySQL 只让一个成功，
另一个拿到 1062。所以这里也顺带并发打一次。
"""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import engine
from app.models.side_effect import SideEffectKind
from app.services.side_effect_service import SideEffectGuard
from sqlalchemy import text

TAG = "selftest-order:999"


def _cleanup() -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM side_effect_claims WHERE target = :tag"), {"tag": TAG})


def main() -> int:
    results: list[bool] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))

    _cleanup()
    print("副作用幂等键行为验证：")

    # ① 第一次认领
    guard = SideEffectGuard(
        kind=SideEffectKind.REFUND, agent_run_id=101, target=TAG, ticket_id=1
    )
    check("① 第一次认领成功", guard.claim() is None)
    guard.complete({"executed": True, "refund": {"refund_no": "REFUND-TEST-1"}})

    # ② 同一把键第二次认领被挡
    again = SideEffectGuard(kind=SideEffectKind.REFUND, agent_run_id=101, target=TAG, ticket_id=1)
    existing = again.claim()
    check("② 同一把键第二次认领被唯一约束挡住", existing is not None)
    check(
        "② 重复请求能拿到上次的结果快照（可以如实转述「已经做过了」）",
        bool((existing or {}).get("result")),
        str((existing or {}).get("result"))[:80],
    )

    # ③ 失败 → 释放 → 同一次执行还能重试
    guard3 = SideEffectGuard(kind=SideEffectKind.REFUND, agent_run_id=202, target=TAG, ticket_id=1)
    check("③ 新执行的第一次认领成功", guard3.claim() is None)
    guard3.release()
    guard3_retry = SideEffectGuard(
        kind=SideEffectKind.REFUND, agent_run_id=202, target=TAG, ticket_id=1
    )
    check("③ 失败释放后，同一次执行可以重新认领（不会把自己挡死）", guard3_retry.claim() is None)
    guard3_retry.release()

    # ④ 换一次执行 = 新的意图
    guard4 = SideEffectGuard(kind=SideEffectKind.REFUND, agent_run_id=303, target=TAG, ticket_id=1)
    check("④ 新 agent_run_id 是新键（用户重新发起时允许再走一遍业务规则）", guard4.claim() is None)
    guard4.release()

    # ⑤ 并发：10 个线程抢同一把键，只能有一个成功
    _cleanup()
    winners: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(10)

    def race(index: int) -> None:
        barrier.wait()
        g = SideEffectGuard(kind=SideEffectKind.REFUND, agent_run_id=404, target=TAG, ticket_id=1)
        if g.claim() is None:
            with lock:
                winners.append(index)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(race, range(10)))
    check("⑤ 10 个并发请求抢同一把键：恰好 1 个认领成功", len(winners) == 1, f"成功={len(winners)}")

    _cleanup()
    print(f"\n  通过 {sum(results)}/{len(results)} 项")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
