"""工单服务。

工单是客服系统的核心载体（v2.md §18-§25），这里负责：
    - 创建工单（可关联订单/售后；售后类工单自动补建售后单）
    - 工单消息（用户 / 客服 / Agent / 系统 四类发送者）
    - 状态流转：OPEN → PROCESSING → RESOLVED → CLOSED，以及 WAITING_HUMAN / FAILED
    - 客服领取（assignee_id）、解决、关闭、用户不满意重新打开
    - 工单详情装配（消息 + Agent 执行记录 + Agent 业务事件）
    - **Agent 执行权的抢占与释放**（acquire / release / settle，并发互斥的唯一入口）

为什么"Agent 执行权"必须由 TicketService 管，而不是 AgentService 自己改库
------------------------------------------------------------------------
两个状态职责不同（见 models/ticket.py）：
    tickets.status       业务流程状态：待处理 / 处理中 / 已解决 …
    tickets.agent_status Agent 执行权：此刻有没有 Agent 占着这张工单
把它们放在同一个 Service 里，"工单状态"的所有权才不会被 Agent 模块切碎；
AgentService 只描述"我这次执行是什么结局"，由 TicketService 决定该落成什么状态。
"""

from app.core.database import session_scope
from app.core.exceptions import (
    AgentExecutionError,
    InvalidStateError,
    OrderNotFoundError,
    PermissionDeniedError,
    TicketNotFoundError,
)
from app.core.ids import gen_ticket_no
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.agent_event import AgentEventType
from app.models.agent_run import AgentRunStatus
from app.models.ticket import AgentStatus, TicketStatus
from app.models.ticket_message import SenderType
from app.repositories import (
    agent_run_repository,
    after_sale_repository,
    order_repository,
    ticket_repository,
    user_memory_repository,
)
from app.services import after_sale_service

logger = get_logger(__name__)


# ======================================================================
# 创建
# ======================================================================
def create_ticket(
    *,
    user_id: int,
    content: str,
    order_id: int | None = None,
    after_sale_id: int | None = None,
    type: str | None = None,
    title: str | None = None,
) -> dict:
    """创建工单。

    v2.md §18.2 / §46：
        带 order_id 时必须校验"订单存在 + 属于当前用户"；
        如果是退款类工单且还没建售后单，自动补建一张（保持"售后是业务对象"的模型）。
    """
    order_snapshot: dict | None = None

    with session_scope() as db:
        if order_id is not None:
            order_snapshot = order_repository.get_by_id(db, order_id)
            if order_snapshot is None:
                raise OrderNotFoundError(f"订单 {order_id} 不存在")
            if order_snapshot["user_id"] != user_id:
                raise PermissionDeniedError("无权为他人的订单创建工单")

        if after_sale_id is not None:
            sale = after_sale_repository.get_by_id(db, after_sale_id)
            if sale is None:
                raise InvalidStateError(f"售后单 {after_sale_id} 不存在")
            if sale["user_id"] != user_id:
                raise PermissionDeniedError("无权关联他人的售后单")
            if order_id is not None and sale["order_id"] != order_id:
                raise InvalidStateError("售后单与工单订单不匹配")
            if order_id is None:
                order_id = sale["order_id"]
                order_snapshot = order_repository.get_by_id(db, order_id)
                if order_snapshot is None:
                    raise OrderNotFoundError(f"订单 {order_id} 不存在")
                if order_snapshot["user_id"] != user_id:
                    raise PermissionDeniedError("无权为他人的订单创建工单")

        ticket = ticket_repository.create(
            db,
            ticket_no=gen_ticket_no(),
            user_id=user_id,
            content=content,
            title=title,
            order_id=order_id,
            after_sale_id=after_sale_id,
            type=type,
        )
        # 首条消息 = 用户的原始描述（工单对话从这句开始）
        ticket_repository.add_message(
            db,
            ticket_id=ticket["id"],
            sender_type=SenderType.USER,
            sender_id=user_id,
            content=content,
        )

    # 退款类工单但没有售后单 → 补建（在另一个事务里，避免长事务）
    if after_sale_id is None and order_id is not None and type == "REFUND":
        existing = _find_active_after_sale(order_id)
        if existing is not None:
            with session_scope() as db:
                model = ticket_repository.get_model(db, ticket["id"])
                if model is not None:
                    model.after_sale_id = existing["id"]
                    db.flush()
        if existing is None:
            try:
                sale = after_sale_service.create_after_sale(
                    user_id=user_id, order_id=order_id, reason=content
                )
                with session_scope() as db:
                    ticket_repository.update_fields(db, ticket["id"], type="REFUND")
                    model = ticket_repository.get_model(db, ticket["id"])
                    if model is not None:
                        model.after_sale_id = sale["id"]
                        db.flush()
                logger.info("工单自动补建售后单：ticket=%s after_sale=%s", ticket["id"], sale["id"])
            except Exception as exc:  # noqa: BLE001 —— 售后建不出来不该让工单创建失败
                logger.warning("工单补建售后单失败（不影响工单）：%s", exc)

    logger.info("创建工单：ticket_no=%s user=%s order=%s", ticket["ticket_no"], user_id, order_id)
    return get_ticket_detail(ticket["id"], user_id=user_id)


# ======================================================================
# 查询
# ======================================================================
def list_my_tickets(*, user_id: int, limit: int = 20, offset: int = 0, status: str | None = None) -> dict:
    """用户的"我的工单"列表（可按状态筛），带分页信息。"""
    with session_scope() as db:
        items, total = ticket_repository.list_tickets(
            db, user_id=user_id, limit=limit, offset=offset, status=status
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def list_tickets_for_staff(
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    assignee_id: int | None = None,
    unassigned: bool = False,
    keyword: str | None = None,
) -> dict:
    """客服工作台列表：支持按状态/归属/关键词过滤（v2.md §23.1）。"""
    with session_scope() as db:
        items, total = ticket_repository.list_tickets(
            db,
            limit=limit,
            offset=offset,
            status=status,
            statuses=statuses,
            assignee_id=assignee_id,
            unassigned=unassigned,
            keyword=keyword,
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_ticket_detail(ticket_id: int, *, user_id: int | None = None) -> dict:
    """工单详情：工单 + 消息 + 最近一次 Agent 执行 + 事件 + 历史执行记录。

    user_id 不为 None 时校验归属（用户只能看自己的工单；客服传 None）。
    """
    with session_scope() as db:
        ticket = ticket_repository.get_by_id(db, ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
        if user_id is not None and ticket["user_id"] != user_id:
            raise PermissionDeniedError("无权查看他人的工单")

        messages = ticket_repository.list_messages(db, ticket_id)
        runs = agent_run_repository.list_runs(db, ticket_id)
        latest_run = runs[0] if runs else None
        events = (
            agent_run_repository.list_events(db, latest_run["id"]) if latest_run else []
        )

    return {
        **ticket,
        "messages": messages,
        "agent_run": latest_run,
        "agent_runs": runs,
        "agent_events": events,
    }


def get_ticket_raw(ticket_id: int, *, user_id: int | None = None) -> dict:
    """只要工单本身（Agent 工具有时会用）。"""
    with session_scope() as db:
        ticket = ticket_repository.get_by_id(db, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
    if user_id is not None and ticket["user_id"] != user_id:
        raise PermissionDeniedError("无权访问他人的工单")
    return ticket


# ======================================================================
# 消息
# ======================================================================
def add_message(
    *,
    ticket_id: int,
    content: str,
    sender_type: str,
    sender_id: int | None = None,
    user_id: int | None = None,
    dedup_key: str | None = None,
) -> dict:
    """追加一条工单消息。

    user_id 不为 None 表示"用户视角发言"：校验工单归属，
    并且用户发言后如果工单处于 RESOLVED/CLOSED，自动重新打开（v2.md §67：用户不满意可继续沟通）。
    """
    with session_scope() as db:
        ticket = ticket_repository.get_by_id(db, ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
        if user_id is not None and ticket["user_id"] != user_id:
            raise PermissionDeniedError("无权在他人的工单里发言")

        message = ticket_repository.add_message(
            db,
            ticket_id=ticket_id,
            sender_type=sender_type,
            content=content,
            sender_id=sender_id,
            dedup_key=dedup_key,
        )

        # 用户回复 → 已解决/已关闭的工单重新进入处理中
        if sender_type == SenderType.USER and ticket["status"] in (
            TicketStatus.RESOLVED,
            TicketStatus.CLOSED,
        ):
            ticket_repository.update_status(db, ticket_id, TicketStatus.PROCESSING)
            logger.info("用户回复，工单重新打开：ticket_id=%s", ticket_id)

    return message


# ======================================================================
# 状态流转
# ======================================================================
def assign_ticket(*, ticket_id: int, assignee_id: int | None) -> dict:
    """客服领取 / 取消领取工单（v2.md §23.1）。"""
    with session_scope() as db:
        ticket = ticket_repository.get_by_id(db, ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"工单 {ticket_id} 不存在")

        updated = ticket_repository.update_fields(db, ticket_id, assignee_id=assignee_id)

    logger.info("工单领取变更：ticket_id=%s assignee=%s", ticket_id, assignee_id)
    return updated


def update_ticket_fields(ticket_id: int, **fields) -> dict:
    """改工单的分类/优先级/标题/归属（Agent 的 update_ticket 工具也走这里）。

    只透传仓储支持的字段；传了不认识的键会被 update_fields 忽略。
    状态不在这里改 —— 状态流转有专门的方法（mark_processing 等），
    混在一起会让"谁能把工单改成什么状态"变得不可审计。
    """
    with session_scope() as db:
        updated = ticket_repository.update_fields(db, ticket_id, **fields)
    if updated is None:
        raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
    return updated


def mark_processing(ticket_id: int) -> dict:
    """工单进入处理中（Agent 开始跑 / 驳回后交回人工）。"""
    return _set_status(ticket_id, TicketStatus.PROCESSING)


def mark_waiting_human(ticket_id: int) -> dict:
    """工单进入等待人工（Agent 判定需要人工介入）。

    注意这是**业务状态**；"执行权"那一路由 settle_agent_execution 写成
    agent_status=WAITING_HUMAN。两者要一起落，否则工作台会显示成
    "在处理中但 Agent 没在跑"这种自相矛盾的样子。
    """
    return _set_status(ticket_id, TicketStatus.WAITING_HUMAN)


def mark_failed(ticket_id: int) -> dict:
    """工单进入失败态（Agent 执行失败，转客服处理）。"""
    return _set_status(ticket_id, TicketStatus.FAILED)


def resolve_ticket(*, ticket_id: int, solution: str, operator_id: int | None = None,
                   agent_run_id: int | None = None, agent_reply: bool = False) -> dict:
    """标记已解决（等用户确认后关闭，v2.md §67）。"""
    with session_scope() as db:
        ticket = ticket_repository.get_for_update(db, ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
        if agent_run_id is None:
            active = agent_run_repository.find_active_run(db, ticket_id)
            if active:
                raise InvalidStateError("请先接管或结束 Agent 任务，再手动解决工单")
        finish_key = f"run:{agent_run_id}:ticket:{ticket_id}:resolved" if agent_run_id is not None else None
        if finish_key and ticket_repository.has_message_key(db, finish_key):
            return get_ticket_detail(ticket_id)  # 不覆盖用户重新打开后的状态，也不重复写消息。
        if ticket["status"] == TicketStatus.CLOSED:
            raise InvalidStateError("工单已关闭")

        ticket_repository.update_status(
            db, ticket_id, TicketStatus.RESOLVED, resolved_at=utcnow()
        )
        user_memory_repository.save_ticket_summary(db, ticket=ticket, solution=solution)
        if agent_reply:
            ticket_repository.add_message(db, ticket_id=ticket_id, sender_type=SenderType.AGENT,
                content=solution, dedup_key=f"run:{agent_run_id}:ticket:{ticket_id}:reply" if finish_key else None)
        ticket_repository.add_message(
            db,
            ticket_id=ticket_id,
            sender_type=SenderType.SYSTEM,
            sender_id=operator_id,
            content=f"工单已解决：{solution}",
            dedup_key=finish_key,
        )

    logger.info("工单已解决：ticket_id=%s", ticket_id)
    return get_ticket_detail(ticket_id)


def close_ticket(
    *,
    ticket_id: int,
    comment: str | None = None,
    operator_id: int | None = None,
    user_id: int | None = None,
) -> dict:
    """关闭工单。

    两种调用视角，**权限都必须在这里判**（不能只靠 Router 记得传对参数）：
        user_id 不为 None  → 用户视角：只能关自己的工单（别人的 → 403）
        user_id 为 None    → 客服/管理员视角：可以关任何工单（他们本来就要处理全量工单）

    为什么这个参数不能省：`POST /tickets/{id}/close` 对"用户确认解决"和"客服结单"
    是同一个路径，只按角色放行的话，任何登录用户都能把别人的工单关掉 ——
    归属校验必须落在 Service 这一层。
    """
    with session_scope() as db:
        ticket = ticket_repository.get_for_update(db, ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
        if user_id is not None and ticket["user_id"] != user_id:
            raise PermissionDeniedError("无权关闭他人的工单")
        if agent_run_repository.find_active_run(db, ticket_id):
            raise InvalidStateError("Agent 任务尚未结束，暂不能关闭工单")

        ticket_repository.update_status(db, ticket_id, TicketStatus.CLOSED, closed_at=utcnow())
        ticket_repository.add_message(
            db,
            ticket_id=ticket_id,
            sender_type=SenderType.SYSTEM,
            sender_id=operator_id,
            content=comment or "工单已关闭",
        )

    logger.info("工单已关闭：ticket_id=%s operator=%s", ticket_id, operator_id)
    return get_ticket_detail(ticket_id)


def reopen_ticket(*, ticket_id: int, user_id: int) -> dict:
    """用户对处理结果不满意 → 重新打开（v2.md §67）。"""
    with session_scope() as db:
        ticket = ticket_repository.get_by_id(db, ticket_id)
        if ticket is None:
            raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
        if ticket["user_id"] != user_id:
            raise PermissionDeniedError("无权操作他人的工单")

        ticket_repository.update_status(db, ticket_id, TicketStatus.PROCESSING)

    logger.info("工单重新打开：ticket_id=%s", ticket_id)
    return get_ticket_detail(ticket_id)


# ======================================================================
# Agent 执行权（并发互斥的唯一入口）
# ======================================================================
def acquire_agent_execution(ticket_id: int, *, agent_run_id: int | None = None) -> bool:
    """抢占"Agent 执行权"：只有空闲（agent_status = IDLE）的工单能抢到。

    正确写法（一条原子 UPDATE，靠 affected_rows 判断）：

        UPDATE tickets SET agent_status = 'RUNNING'
        WHERE id = ? AND agent_status = 'IDLE'

    错误写法（先查后改，会出竞态 —— 两个请求都查到 IDLE，都去建 AgentRun）：

        if not is_running(ticket_id):   # ← 两个请求都走到这里
            create_run(ticket_id)       # ← 于是有了两条活跃 AgentRun

    返回 True 表示抢到了，False 表示"别人正在处理这张工单"。
    抢到之后调用方必须用 try/finally 保证释放（见 agent_service.process_ticket）。
    """
    with session_scope() as db:
        affected = ticket_repository.acquire_agent_execution(
            db, ticket_id, expected=(AgentStatus.IDLE,), agent_run_id=agent_run_id
        )
    if affected:
        logger.info("抢到 Agent 执行权：ticket_id=%s run=%s", ticket_id, agent_run_id)
        return True

    state = get_agent_state(ticket_id)
    logger.info(
        "未抢到 Agent 执行权（已有执行占用）：ticket_id=%s 当前 agent_status=%s run=%s",
        ticket_id,
        (state or {}).get("agent_status"),
        (state or {}).get("agent_run_id"),
    )
    return False


def acquire_agent_execution_for_resume(ticket_id: int, *, agent_run_id: int) -> bool:
    """人工审批后恢复执行时用的抢占：只有"停在人工确认点的那一次"能恢复。

    和 process 路径抢的是同一把锁（agent_status），区别只是合法旧状态：
        process  IDLE          → RUNNING
        resume   WAITING_HUMAN → RUNNING（且必须还是同一条 run）

    这样"客服连点两次批准"也只有一个请求能真正把图恢复起来。
    """
    with session_scope() as db:
        affected = ticket_repository.acquire_agent_execution(
            db, ticket_id, expected=(AgentStatus.WAITING_HUMAN,), agent_run_id=agent_run_id,
            require_same_run=True,
        )
    if affected:
        logger.info("抢到 Agent 恢复执行权：ticket_id=%s run=%s", ticket_id, agent_run_id)
    return bool(affected)


def release_agent_execution(ticket_id: int, *, agent_run_id: int | None) -> bool:
    """释放执行权 → IDLE。正常完成 / 失败 / 驳回 都走这里（在 finally 里）。"""
    return settle_agent_execution(ticket_id, agent_run_id=agent_run_id, idle=True)


def settle_agent_execution(
    ticket_id: int,
    *,
    agent_run_id: int | None,
    idle: bool = True,
    resolvable_states: tuple[str, ...] = (AgentStatus.RUNNING,),
) -> bool:
    """本次执行收尾：把执行权落到 IDLE 或 WAITING_HUMAN。

    参数
    ----
    idle                True → 落到 IDLE（执行结束，工单可以被重新处理）
                        False → 落到 WAITING_HUMAN（停在人工确认点，等客服审批）
    resolvable_states   允许从哪些状态收尾。默认只允许 RUNNING（正常路径）；
                        人工审批后的恢复路径要允许 RUNNING 和 WAITING_HUMAN 两种
                        （恢复过程中再次挂起时，工单本来就还在 WAITING_HUMAN）。

    返回 False 说明"这次执行已经不再是执行权的持有者"（例如超时被别人接管了），
    调用方不要覆盖别人的状态 —— 这正是 agent_run_id 归属校验存在的意义。
    """
    next_status = AgentStatus.IDLE if idle else AgentStatus.WAITING_HUMAN
    with session_scope() as db:
        affected = ticket_repository.settle_agent_execution(
            db,
            ticket_id,
            agent_run_id=agent_run_id,
            final_status=next_status,
            releasable=resolvable_states,
        )
    if affected:
        logger.info(
            "Agent 执行权收尾：ticket_id=%s run=%s → %s", ticket_id, agent_run_id, next_status
        )
    else:
        logger.warning(
            "Agent 执行权收尾未生效（可能已被接管）：ticket_id=%s run=%s 目标=%s",
            ticket_id,
            agent_run_id,
            next_status,
        )
    return bool(affected)


def get_agent_state(ticket_id: int) -> dict | None:
    """当前执行权状态（只读）：{"agent_status": ..., "agent_run_id": ...}。"""
    with session_scope() as db:
        return ticket_repository.get_agent_state(db, ticket_id)


# ======================================================================
# 人工接管（HITL 的第三个出口：批准 / 驳回 / **接管**）
# ======================================================================
def takeover_ticket(*, ticket_id: int, operator_id: int, comment: str | None = None) -> dict:
    """短事务接管等待中的任务；正在执行的任务不能由页面强行打断。"""
    from app.services.agent_task_service import lock_ticket_run, conflict
    from app.services.refund_service import _validate_operator
    with session_scope() as db:
        ticket, run = lock_ticket_run(db, ticket_id)
        _validate_operator(db, operator_id)
        if run and run.status in (AgentRunStatus.PENDING, AgentRunStatus.RUNNING):
            conflict("Agent 正在排队或执行，请等待完成后再接管")
        if ticket.assignee_id not in (None, operator_id):
            conflict("工单已由其他客服接管")
        if ticket.status == TicketStatus.CLOSED:
            conflict("工单已关闭")
        if ticket.assignee_id == operator_id and not run:
            return get_ticket_detail(ticket_id)
        ticket.assignee_id = operator_id
        if run and run.status == AgentRunStatus.WAITING_HUMAN:
            run.status = AgentRunStatus.REJECTED
            run.finished_at = utcnow()
            run.worker_id = None
            run.lease_until = None
            run.error = comment or "客服已接管"
        ticket.agent_run_id = None
        ticket.agent_status = AgentStatus.IDLE
        ticket.status = TicketStatus.PROCESSING
        ticket_repository.add_message(db, ticket_id=ticket_id, sender_type=SenderType.SYSTEM,
            sender_id=operator_id, content="客服已接管，后续由人工处理" + (f"：{comment}" if comment else ""))
    return get_ticket_detail(ticket_id)


def assign_ticket_if_unassigned(ticket_id: int, assignee_id: int) -> bool:
    """条件更新抢归属（`WHERE assignee_id IS NULL`），返回是否抢到。

    单独抽出来是为了让"接管的互斥点"只有一处实现 —— 它必须和
    `agent_status` 的抢占一样是**单条带条件的 UPDATE**。
    """
    with session_scope() as db:
        return ticket_repository.assign_ticket(
            db, ticket_id, assignee_id, only_if_unassigned=True
        )



def get_active_run(ticket_id: int) -> dict | None:
    """工单当前活跃的 AgentRun（在跑 / 停在人工确认点）。抢占失败时返回它。"""
    with session_scope() as db:
        return agent_run_repository.find_active_run(db, ticket_id)


def reap_stale_execution(
    ticket_id: int,
    *,
    stale_seconds: int,
    still_expected_run_id: int | None,
) -> dict | None:
    """收掉"僵尸执行"：进程被杀 / 容器重启留下的、心跳早就停了的 RUNNING。

    为什么必须有这一步：执行权是数据库里的一个字段，进程崩溃时没人去 finally 释放它，
    工单就会永久卡在 RUNNING（用户再也点不动"开始处理"）。
    所以抢占失败时先判断"占着锁的那次执行是不是还活着"：

        心跳很新        → 真的在跑，老实返回当前 Run（不抢）
        心跳超时/无 run → 判定为僵尸：把执行权回收成 IDLE，并把它标成 FAILED

    返回：被收掉的僵尸 Run（没有可回收的执行则为 None）。

    并发安全性：回收动作本身也是一条带条件的原子 UPDATE
    （`WHERE agent_status='RUNNING' AND agent_run_id=?`），
    多个请求同时来回收时只有一个能改动成功，不会把别人的新执行误伤。
    """
    with session_scope() as db:
        state = ticket_repository.get_agent_state(db, ticket_id)
        if state is None or state["agent_status"] != AgentStatus.RUNNING:
            return None

        # 重试期间 run 可暂时为 FAILED，但新鲜心跳仍表示执行权有人持有。
        current_run_id = state["agent_run_id"]
        run = agent_run_repository.get_run(db, current_run_id) if current_run_id is not None else None
        if run is not None and _is_run_alive(run, stale_seconds=stale_seconds):
            return None
        if current_run_id is None:
            ticket = ticket_repository.get_by_id(db, ticket_id)
            acquired_at = (ticket or {}).get("updated_at")
            if acquired_at is not None and (utcnow() - acquired_at).total_seconds() < stale_seconds:
                # 抢占与创建 run 是两次事务，不能把中间窗口当成进程已崩溃。
                return None

        # 注意 still_expected_run_id 的用法：如果调用方看到的是"某一次具体执行"，
        # 就必须确认现在占锁的还是那一次，避免把刚刚抢到锁的新执行当成僵尸。
        if (
            still_expected_run_id is not None
            and current_run_id is not None
            and current_run_id != still_expected_run_id
        ):
            return None

        affected = ticket_repository.settle_agent_execution(
            db,
            ticket_id,
            agent_run_id=current_run_id,
            final_status=AgentStatus.IDLE,
            releasable=(AgentStatus.RUNNING,),
        )
        if not affected:
            return None

        if run is not None:
            agent_run_repository.mark_finished(
                db,
                run["id"],
                status=AgentRunStatus.FAILED,
                finished_at=utcnow(),
                error="执行心跳超时（进程退出或崩溃），已由后续请求接管执行权",
            )
            agent_run_repository.add_event(
                db,
                agent_run_id=run["id"],
                event_type=AgentEventType.FAILED,
                message="执行心跳超时，判定为僵尸执行并回收执行权",
            )

    logger.warning(
        "回收僵尸 Agent 执行：ticket_id=%s 原 run=%s（心跳超时 %s 秒）",
        ticket_id,
        current_run_id,
        stale_seconds,
    )
    # 工单上记着 run_id、但那条 AgentRun 已经查不到（数据被清过/手工造过）：
    # 锁照样回收了，只是没有记录可标记 —— 如实返回"收了个空壳"，不假装标了 FAILED
    return run if run is not None else {"id": current_run_id}


def _is_run_alive(run: dict, *, stale_seconds: int) -> bool:
    """心跳是否还新鲜（判定"这次执行是不是已经死了"）。

    两个必须处理的细节：

    1. 老数据没有 alive_at → 退回 started_at（迁移时也用 COALESCE 补过）。
    2. **心跳时间在未来** → 视为新鲜，并打一条警告。
       这不是理论问题：应用统一写 UTC（见 core/time.py），而 MySQL 的 NOW() 是
       服务器本地时间。有人用 SQL 手工改过、或哪天有人误用 NOW() 写库，
       算出来的"心跳年龄"就会是负数 —— 负数意味着"最新鲜的心跳"，
       而不是"很久没动了"，绝不能把它当成僵尸执行去抢别人的工单。
    """
    heartbeat = run.get("alive_at") or run.get("started_at")
    if heartbeat is None:
        return False

    age = (utcnow() - heartbeat).total_seconds()
    if age < 0:
        logger.warning(
            "AgentRun %s 的心跳时间在未来（%s），已按「仍在执行」处理；"
            "通常是写入方用了本地时间而不是 UTC",
            run.get("id"),
            heartbeat,
        )
        return True
    return age < stale_seconds


# ======================================================================
# 工作台统计
# ======================================================================
def get_stats() -> dict:
    """客服工作台的数据概览：各业务状态的工单数（{状态: 条数}）。"""
    with session_scope() as db:
        return ticket_repository.count_by_status(db)


# ======================================================================
# 内部
# ======================================================================
def _set_status(ticket_id: int, status: str) -> dict:
    """统一的状态落地入口（mark_processing / mark_waiting_human / mark_failed 都走它）。

    抽出来的原因：这三个方法除了目标状态以外行为完全一样，
    将来要给状态流转加审计/事件推送时，只需要改这一处。
    """
    with session_scope() as db:
        updated = ticket_repository.update_status(db, ticket_id, status)
    if updated is None:
        raise TicketNotFoundError(f"工单 {ticket_id} 不存在")
    return updated


def _find_active_after_sale(order_id: int) -> dict | None:
    with session_scope() as db:
        return after_sale_repository.find_active_by_order(db, order_id)
