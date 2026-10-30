"""Agent 输入组装、错误分类与兼容入口。

任务生命周期在 agent_task_service；图执行在 app.worker。API 不持有 Checkpoint。
"""

import json


from app.agent.state import AgentState
from app.core.database import session_scope
from app.core.logging import get_logger
from app.repositories import agent_run_repository, ticket_repository
from app.services import ticket_service

logger = get_logger(__name__)

# 一张工单一次执行最多走多少步（防止模型或图陷入死循环）
RECURSION_LIMIT = 25

#: 这些关键字出现时认为"这次失败值得重试"（网络/限流/依赖抖动）
_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "connect",
    "connection",
    "rate limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "temporarily",
    "unavailable",
    "overloaded",
    "reset by peer",
    "ssl",
    "eof",
)


# ======================================================================
# 启动一次 Agent 执行
# ======================================================================
async def process_ticket(*, ticket_id: int, user_id: int, content: str | None = None,
                         operator_id: int | None = None) -> dict:
    """兼容调用入口：只提交任务，不在 API 进程执行图。"""
    from app.services.agent_task_service import submit_agent_run
    return submit_agent_run(ticket_id=ticket_id, user_id=user_id)


async def resume_with_decision(*, ticket_id: int, operator_id: int, approved: bool,
                               expected_run_id: int, review_round: int,
                               comment: str | None = None) -> dict:
    """兼容调用入口：持久化审批，图只能由 worker 恢复。"""
    from app.services.agent_task_service import submit_approval
    return submit_approval(ticket_id=ticket_id, operator_id=operator_id, approved=approved,
                           expected_run_id=expected_run_id, review_round=review_round, comment=comment)


def find_waiting_run(ticket_id: int) -> dict | None:
    """这张工单此刻**真的在等**的那次执行（approve/reject 要靠它恢复图）。

    注意"真的在等"是复合判据：AgentRun.status=WAITING_HUMAN **且** 工单指针
    还指着它。只看状态会拿到历史遗留的旧 run（详见仓储里的说明）。
    """
    with session_scope() as db:
        return agent_run_repository.find_waiting_run(db, ticket_id)


def list_runs(ticket_id: int) -> list[dict]:
    """这张工单的全部执行记录（倒序 = 最新在前）。

    同一任务重试更新原记录，重新发起才新增记录。
    所以客服工作台看到的"执行记录"是一个列表，而不是一条不断被改写的记录。
    """
    with session_scope() as db:
        return agent_run_repository.list_runs(db, ticket_id)


def active_run(ticket_id: int) -> dict | None:
    """对外暴露"当前活跃执行"，供接口/脚本判断工单是否正在被 Agent 处理。"""
    return ticket_service.get_active_run(ticket_id)


# ======================================================================
# 执行权：抢占成功后的内部动作
# ======================================================================
def is_retryable_error(exc: Exception) -> bool:
    """判定异常是否值得重试（保守：拿不准就不重试）。

    不可恢复的业务异常有一个共同特征：它们是 BusinessError，
    且 code 不是供应商侧的临时故障 —— 这类直接失败，把原因讲清楚更有价值。
    """
    from app.core.exceptions import BusinessError

    text = f"{type(exc).__name__}: {exc}".lower()

    # 业务规则类错误：订单不存在、退款条件不满足、权限不足……重试无意义
    if isinstance(exc, BusinessError):
        # AgentExecutionError 是"包了一层"的执行失败，要看底层原因
        if exc.code not in ("AGENT_EXECUTION_FAILED",):
            return False

    return any(marker in text for marker in _RETRYABLE_MARKERS)


def _brief(exc: Exception, limit: int = 120) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] + ("…" if len(text) > limit else "")


# ======================================================================
# 内部
# ======================================================================
def _build_initial_state(
    *,
    ticket: dict,
    agent_run_id: int,
    thread_id: str,
    content: str,
) -> AgentState:
    """组装初始 State（工单内容 + 最近对话 + 关联业务对象 id）。"""
    from app.agent.version import execution_spec
    recent_messages = _recent_messages(ticket["id"])

    # 没有 after_sale_id 时，尝试按订单找一张进行中的售后单，让 Agent 一上来就知道上下文
    after_sale_id = ticket.get("after_sale_id")
    if after_sale_id is None and ticket.get("order_id"):
        from app.repositories import after_sale_repository

        with session_scope() as db:
            sale = after_sale_repository.find_active_by_order(db, ticket["order_id"])
        after_sale_id = sale["id"] if sale else None

    return {
        "flow_version": execution_spec()["version"],
        "user_id": ticket["user_id"],
        "ticket_id": ticket["id"],
        "order_id": ticket.get("order_id"),
        "after_sale_id": after_sale_id,
        "thread_id": thread_id,
        "agent_run_id": agent_run_id,
        "content": content,
        "conversation": recent_messages,
        "retrieved_context": [],
        "references": [],
        "tool_results": [],
        "used_rag": False,
        "used_tool": False,
        "decision": None,
        "analysis": None,
        "result": None,
        "need_human": False,
        "human_approved": None,
        "human_comment": None,
        "human_operator_id": None,
        "business_action": None,
        "action_result": None,
        "status": "PENDING",
    }


def _recent_messages(ticket_id: int, limit: int = 20) -> list[dict]:
    with session_scope() as db:
        return ticket_repository.list_recent_messages(db, ticket_id, limit=limit)


def get_run(agent_run_id: int) -> dict | None:
    """按 id 取一条执行记录（供接口/脚本读取，避免它们直接 import 仓储）。"""
    with session_scope() as db:
        return agent_run_repository.get_run(db, agent_run_id)


def describe_for_log(result: dict) -> str:
    """把结果压成一行日志（排查并发问题时很省事）。"""
    run = result.get("agent_run") or {}
    return json.dumps(
        {
            "ticket_id": run.get("ticket_id"),
            "run_id": run.get("id"),
            "status": result.get("status"),
            "reused": result.get("reused"),
        },
        ensure_ascii=False,
    )
