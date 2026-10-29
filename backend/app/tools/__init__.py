"""tools：Agent 可以调用的业务能力（v2.md §36）。

    order_tools.py       query_order / query_order_items
    logistics_tools.py   query_logistics
    after_sale_tools.py  query_after_sale / create_after_sale
    refund_tools.py      apply_refund / check_refund_eligibility
    ticket_tools.py      query_ticket_history / update_ticket
    user_tools.py        query_user / query_user_memory

两条铁律：
    1. **Tool 必须对应真实业务能力**（v2.md §24）—— 不为展示技术造工具；
    2. **Tool 不碰数据库**（v2.md §37）：Tool → Service → Repository → MySQL。

每个工具都返回结构化结果（成功 data / 失败 error code），
模型看到的是业务错误而不是 Python 堆栈。
"""

from app.tools.after_sale_tools import create_after_sale, query_after_sale
from app.tools.logistics_tools import query_logistics
from app.tools.order_tools import cancel_order, query_order, query_order_items
from app.tools.refund_tools import apply_refund, check_refund_eligibility
from app.tools.ticket_tools import query_ticket_history, update_ticket
from app.tools.user_tools import query_user, query_user_memory

# 查询类工具（图里的 execute_tools 节点按 AgentDecision 的开关调用它们）
QUERY_TOOLS = {
    "query_order": query_order,
    "query_order_items": query_order_items,
    "query_logistics": query_logistics,
    "query_after_sale": query_after_sale,
    "query_ticket_history": query_ticket_history,
    "query_user": query_user,
    "query_user_memory": query_user_memory,
}

# 业务操作类工具（只有满足业务规则、必要时经人工批准后才会被调用）
ACTION_TOOLS = {
    "cancel_order": cancel_order,
    "apply_refund": apply_refund,
    "create_after_sale": create_after_sale,
    "update_ticket": update_ticket,
}

ALL_TOOLS = {**QUERY_TOOLS, **ACTION_TOOLS, "check_refund_eligibility": check_refund_eligibility}

__all__ = ["ACTION_TOOLS", "ALL_TOOLS", "QUERY_TOOLS"]
