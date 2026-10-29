"""清理 pytest 留下的测试数据（`pytest_*` 账号及其整条业务链）。

    cd backend
    uv run python scripts/cleanup_test_data.py            # 先看会删什么（dry-run，默认）
    uv run python scripts/cleanup_test_data.py --apply    # 真的删
    uv run python scripts/cleanup_test_data.py --apply --keep-products

为什么需要它
------------
`tests/` 里的用例是**真写库**的（要的正是真实事务、唯一约束与条件更新的行为），
所以每跑一轮 pytest 就会在 `shop_ticket` 里留下约 90 个 `pytest_*` 账号，
以及它们的购物车、订单、支付、售后、退款、工单、消息、Agent 执行与事件。

项目**没有物理外键**（表之间靠 Service 保证一致性），所以删除必须自己按顺序来：
从最外层的从表往里删，最后删账号 —— 顺序错了就会留下孤儿行。

安全边界
--------
只删 `users.username LIKE 'pytest\\_%'` 的账号及其名下的业务数据。
演示账号（alice / bob / service01 / admin）与手工造的数据一概不动。
默认 dry-run，必须显式 `--apply` 才真的删。

注意：`knowledge_documents` 里 category='pytest' 的文档（pytest 知识库生命周期用例
正常会自己删掉，异常中断时可能残留）也会一并清理。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.database import session_scope  # noqa: E402

#: 测试账号的判据（conftest 里所有 fixture 都用 pytest_ 前缀注册）
TEST_USER_LIKE = "pytest\\_%"

#: 副作用幂等键有两条关联路径（ticket_id / agent_run_id），两条都要清
_DEDUP_KEY_BY_TICKET_SQL = (
    "DELETE c FROM side_effect_claims c "
    "JOIN tickets t ON t.id = c.ticket_id "
    "JOIN users u ON u.id = t.user_id "
    f"WHERE u.username LIKE '{TEST_USER_LIKE}'"
)
_DEDUP_KEY_BY_RUN_SQL = (
    "DELETE c FROM side_effect_claims c "
    "JOIN agent_runs r ON r.id = c.agent_run_id "
    "JOIN users u ON u.id = r.user_id "
    f"WHERE u.username LIKE '{TEST_USER_LIKE}'"
)

#: 删除顺序：从最外层的从表往里删，最后删账号。
#: 每一项是 (说明, SQL)。全部以"测试账号"或"测试文档"为锚点，
#: 不用"删全表"，避免误伤演示数据。
STEPS: list[tuple[str, str]] = [
    (
        "Agent 事件（agent_run_events）",
        "DELETE e FROM agent_run_events e "
        "JOIN agent_runs r ON r.id = e.agent_run_id "
        "JOIN users u ON u.id = r.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    # 幂等键必须早于 tickets / agent_runs 删（它引用了两者的 id）
    ("副作用幂等键（按 ticket_id 关联）", _DEDUP_KEY_BY_TICKET_SQL),
    ("副作用幂等键（按 agent_run_id 关联）", _DEDUP_KEY_BY_RUN_SQL),
    (
        "Agent 执行记录（agent_runs）",
        "DELETE r FROM agent_runs r JOIN users u ON u.id = r.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "工单消息（ticket_messages）",
        "DELETE m FROM ticket_messages m JOIN tickets t ON t.id = m.ticket_id "
        "JOIN users u ON u.id = t.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "工单（tickets）",
        "DELETE t FROM tickets t JOIN users u ON u.id = t.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "退款流水（refund_records）",
        "DELETE r FROM refund_records r JOIN users u ON u.id = r.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "售后单（after_sales）",
        "DELETE a FROM after_sales a JOIN users u ON u.id = a.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "物流（logistics）",
        "DELETE l FROM logistics l JOIN orders o ON o.id = l.order_id "
        "JOIN users u ON u.id = o.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "支付流水（payments）",
        "DELETE p FROM payments p JOIN orders o ON o.id = p.order_id "
        "JOIN users u ON u.id = o.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "订单明细（order_items）",
        "DELETE i FROM order_items i JOIN orders o ON o.id = i.order_id "
        "JOIN users u ON u.id = o.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "订单（orders）",
        "DELETE o FROM orders o JOIN users u ON u.id = o.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "购物车（cart_items）",
        "DELETE c FROM cart_items c JOIN users u ON u.id = c.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "用户长期记忆（user_memories）",
        "DELETE m FROM user_memories m JOIN users u ON u.id = m.user_id "
        f"WHERE u.username LIKE '{TEST_USER_LIKE}'",
    ),
    (
        "知识分块（knowledge_chunks，category=pytest）",
        "DELETE c FROM knowledge_chunks c "
        "JOIN knowledge_documents d ON d.id = c.document_id "
        "WHERE d.category = 'pytest'",
    ),
    (
        "知识文档（knowledge_documents，category=pytest）",
        "DELETE FROM knowledge_documents WHERE category = 'pytest'",
    ),
    (
        "测试账号（users）",
        f"DELETE FROM users WHERE username LIKE '{TEST_USER_LIKE}'",
    ),
]

#: 商品不按用户删（管理员建的测试商品 name 带 pytest 前缀）
_PRODUCT_SQL = "DELETE FROM products WHERE name LIKE 'pytest%'"


def _count(db, sql: str) -> int:
    """把删除语句改写成 COUNT，用于 dry-run 预览。

    只支持本项目用到的两种形态（都用到了）：
        DELETE alias FROM a JOIN ... WHERE ...
        DELETE FROM table WHERE ...
    """
    body = sql.strip()
    assert body.upper().startswith("DELETE "), body

    rest = body[len("DELETE ") :].strip()
    if rest.upper().startswith("FROM"):
        # DELETE FROM table WHERE ... → SELECT COUNT(1) FROM table WHERE ...
        return int(db.execute(text("SELECT COUNT(1) AS n " + rest)).scalar() or 0)

    # DELETE alias FROM ... → 去掉别名，直接数 JOIN 出来的行
    _, _, from_clause = rest.partition(" FROM ")
    return int(
        db.execute(text("SELECT COUNT(1) AS n FROM " + from_clause)).scalar() or 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 pytest 留下的测试数据")
    parser.add_argument("--apply", action="store_true", help="真的执行删除（默认只预览）")
    parser.add_argument(
        "--keep-products", action="store_true", help="保留 pytest 建出来的测试商品"
    )
    args = parser.parse_args()

    steps = list(STEPS)
    if not args.keep_products:
        steps.append(("测试商品（products，name 以 pytest 开头）", _PRODUCT_SQL))

    total = 0
    with session_scope() as db:
        for label, sql in steps:
            try:
                affected = _count(db, sql)
            except Exception as exc:  # noqa: BLE001
                print(f"  ?  {label}：无法统计（{type(exc).__name__}: {exc}）")
                continue
            if affected == 0:
                continue
            total += affected
            if args.apply:
                deleted = db.execute(text(sql)).rowcount or 0
                print(f"  ✓ 删除 {deleted:5d} 行  {label}")
            else:
                print(f"  · 将删除 {affected:5d} 行  {label}")

    if total == 0:
        print("没有需要清理的测试数据（库里很干净）。")
        return 0

    if args.apply:
        print(f"\n清理完成，共删除 {total} 行。")
    else:
        print(f"\n以上是**预览**（共 {total} 行）。真的删除请加 --apply。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
