"""工单 + 工单消息数据访问。

本文件里有全项目最关键的几条 SQL：**Agent 执行权的原子抢占 / 释放**
（acquire / release / settle）。它们必须是一条 `UPDATE ... WHERE 旧状态`，
不允许出现"先 SELECT 判断、再 UPDATE"的写法 —— 那是竞态条件。
仓储层只负责把这条语句发出去并如实返回 affected_rows，业务判断在 Service 层。
"""

from datetime import datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.ticket import AgentStatus, Ticket, TicketStatus
from app.models.ticket_message import TicketMessage
from app.repositories.idempotency import insert_once


# ======================================================================
# 工单
# ======================================================================
def create(
    db: Session,
    *,
    ticket_no: str,
    user_id: int,
    content: str,
    title: str | None = None,
    order_id: int | None = None,
    after_sale_id: int | None = None,
    type: str | None = None,
    priority: str = "NORMAL",
    status: str = TicketStatus.OPEN,
    agent_status: str = AgentStatus.IDLE,
) -> dict:
    """新建工单。

    title 缺省取正文前 100 字：工单列表要有个能扫一眼的标题，
    但让用户额外必填一个标题是多余的负担。

    两个状态字段的初值都是"可被 Agent 立即处理"：
        status=OPEN       业务流程上刚提交
        agent_status=IDLE 执行权空闲 —— 只有 IDLE 才能被 acquire 抢到
    `order_id` / `after_sale_id` 是可选的业务上下文，Agent 靠它们查真实数据。
    """
    ticket = Ticket(
        ticket_no=ticket_no,
        user_id=user_id,
        content=content,
        title=title or content[:100],
        order_id=order_id,
        after_sale_id=after_sale_id,
        type=type,
        priority=priority,
        status=status,
        agent_status=agent_status,
    )
    db.add(ticket)
    db.flush()
    db.refresh(ticket)
    return ticket.to_dict()


def get_by_id(db: Session, ticket_id: int) -> dict | None:
    """按主键取工单（字典形式）。不存在返回 None，由 Service 决定抛什么异常。"""
    ticket = db.get(Ticket, ticket_id)
    return ticket.to_dict() if ticket else None


def get_model(db: Session, ticket_id: int) -> Ticket | None:
    """按主键取工单的 **ORM 对象**（需要改字段时用；只读场景请用 get_by_id）。"""
    return db.get(Ticket, ticket_id)


def get_for_update(db: Session, ticket_id: int) -> dict | None:
    """锁定工单，供结单及摘要写入共享同一事务使用。"""
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    return ticket.to_dict() if ticket else None


def list_tickets(
    db: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    user_id: int | None = None,
    assignee_id: int | None = None,
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    keyword: str | None = None,
    unassigned: bool = False,
) -> tuple[list[dict], int]:
    """工单列表（用户端"我的"和客服工作台共用），返回 (当页数据, 总数)。

    过滤条件都是可选的，服务端按角色决定传哪些：
        用户端 → 强制传 user_id（只能看自己的）
        客服端 → user_id 传 None，可以按状态/归属/关键词自由筛

    `keyword` 同时匹配工单号、标题、正文：客服拿到用户口述的"我的单号是 TK2026…"
    或者只记得问题描述，都能搜到。

    返回 total 而不是只返回列表：前端要算页码，少了总数分页就是假的。
    """
    conditions = []
    if user_id is not None:
        conditions.append(Ticket.user_id == user_id)
    if assignee_id is not None:
        conditions.append(Ticket.assignee_id == assignee_id)
    if unassigned:
        conditions.append(Ticket.assignee_id.is_(None))
    if status:
        conditions.append(Ticket.status == status)
    if statuses:
        conditions.append(Ticket.status.in_(statuses))
    if keyword:
        like = f"%{keyword}%"
        conditions.append(
            or_(Ticket.ticket_no.like(like), Ticket.title.like(like), Ticket.content.like(like))
        )

    count_stmt = select(func.count()).select_from(Ticket)
    list_stmt = select(Ticket)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(Ticket.id.desc()).limit(limit).offset(offset))
    return [ticket.to_dict() for ticket in rows], total


def update_status(
    db: Session,
    ticket_id: int,
    status: str,
    *,
    resolved_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> dict | None:
    """改工单的**业务状态**（`tickets.status`），可选带上解决/关闭时间戳。

    **不碰 `agent_status`** —— 执行权有它自己的原子方法（acquire / settle）。
    这两件事分开是有意的：工单可以在"等待人工"的同时"没有 Agent 在跑"，
    把它们耦合起来就会出现"改业务状态顺手把执行权也放了"这类难查的 bug。

    返回 None 表示工单不存在（调用方据此抛 404），不在这里抛异常 ——
    仓储层不决定 HTTP 语义。
    """
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return None

    ticket.status = status
    if resolved_at is not None:
        ticket.resolved_at = resolved_at
    if closed_at is not None:
        ticket.closed_at = closed_at

    db.flush()
    db.refresh(ticket)
    return ticket.to_dict()


def update_fields(
    db: Session,
    ticket_id: int,
    *,
    type: str | None = None,
    priority: str | None = None,
    assignee_id: int | None = None,
    title: str | None = None,
) -> dict | None:
    """改工单的分类 / 优先级 / 归属 / 标题（`type` / `priority` / `assignee_id` / `title`）。

    只更新**明确传了值**的字段（None = 不改），所以它可以安全地当 PATCH 用：
    调用方不用先把整行读出来再回写，"少传一个字段就把别人的值清空"这种事故不会发生。

    同样**不碰 status 和 agent_status**。归属的"抢单"语义请用 `assign_ticket`。
    """
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        return None

    if type is not None:
        ticket.type = type
    if priority is not None:
        ticket.priority = priority
    if assignee_id is not None:
        ticket.assignee_id = assignee_id
    if title is not None:
        ticket.title = title

    db.flush()
    db.refresh(ticket)
    return ticket.to_dict()


def count_by_status(db: Session) -> dict[str, int]:
    """客服工作台的数据概览。"""
    rows = db.execute(select(Ticket.status, func.count()).group_by(Ticket.status)).all()
    return {status: int(count) for status, count in rows}


def compare_assignment(db: Session, ticket_id: int, expected: int | None, assignee_id: int | None) -> bool:
    """原子比较并更新归属：只有归属仍与校验时一致的请求才能领取、释放或转派。"""
    condition = Ticket.assignee_id.is_(None) if expected is None else Ticket.assignee_id == expected
    result = db.execute(update(Ticket).where(Ticket.id == ticket_id, condition).values(assignee_id=assignee_id))
    return result.rowcount == 1


def assign_ticket(
    db: Session,
    ticket_id: int,
    assignee_id: int | None,
    *,
    only_if_unassigned: bool = False,
) -> bool:
    """设置工单归属（客服领取 / 接管）。

    `only_if_unassigned=True` 时是**条件更新**，用于"抢单"：

        UPDATE tickets SET assignee_id = :me
         WHERE id = :ticket_id AND assignee_id IS NULL;

    返回是否真的抢到了那一行。两个客服同时点"接管"时，只有一个能拿到
    affected_rows=1 —— 这就是接管的互斥点（比"先查归属再改"可靠）。
    """
    statement = update(Ticket).where(Ticket.id == ticket_id)
    if only_if_unassigned:
        statement = statement.where(Ticket.assignee_id.is_(None))
    return (db.execute(statement.values(assignee_id=assignee_id)).rowcount or 0) == 1


def get_assignee(db: Session, ticket_id: int) -> int | None:
    """当前归属客服（接管幂等判断用）。"""
    return db.scalar(select(Ticket.assignee_id).where(Ticket.id == ticket_id))


# ======================================================================
# Agent 执行权：原子抢占 / 释放（本项目并发控制的核心 SQL）
# ======================================================================
def acquire_agent_execution(
    db: Session,
    ticket_id: int,
    *,
    expected: tuple[str, ...],
    agent_run_id: int | None = None,
    require_same_run: bool = False,
) -> int:
    """把工单的 Agent 执行权从 `expected` 里的某个状态**原子地**抢到 RUNNING。

        请求 A ─┐
        请求 B ─┼─→ UPDATE tickets SET agent_status='RUNNING'
        请求 C ─┘      WHERE id=? AND agent_status IN ('IDLE')

    MySQL 对同一行的 UPDATE 会串行化执行：只有第一个事务还能匹配到 WHERE，
    其余事务要么看不到那一行（READ COMMITTED）要么匹配失败，
    因此返回的 affected_rows 只会有一个 1。调用方据此判断"我是不是抢到了"。

    返回：受影响行数（1 = 抢到执行权，0 = 没抢到，说明别人正在处理这张工单）。

    注意 WHERE 里的 `expected` 必须包含**抢占者自己的旧状态**：
        process 路径传 ('IDLE',)                    —— 只有空闲的工单能被处理
        approve 路径传 ('WAITING_HUMAN',)           —— 只有等待审批的工单能被恢复
    这样"恢复人工审批"和"用户重新点处理"抢的是同一把锁，不会互相插队。
    """
    statement = (
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.agent_status.in_(expected))
        .values(agent_status=AgentStatus.RUNNING, agent_run_id=agent_run_id)
    )
    if require_same_run:
        statement = statement.where(Ticket.agent_run_id == agent_run_id)
    return db.execute(statement).rowcount or 0


def bind_agent_execution(db: Session, ticket_id: int, *, agent_run_id: int) -> int:
    """只能回填刚抢到、尚未绑定的执行权，不能覆盖另一条 run。"""
    statement = (
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.agent_status == AgentStatus.RUNNING,
               Ticket.agent_run_id.is_(None))
        .values(agent_run_id=agent_run_id)
    )
    return db.execute(statement).rowcount or 0


def release_agent_execution(
    db: Session,
    ticket_id: int,
    *,
    agent_run_id: int | None,
    expected: tuple[str, ...],
    next_status: str = AgentStatus.IDLE,
) -> int:
    """释放执行权（终态收尾），带 `agent_run_id` 归属校验。

    为什么要校验 agent_run_id：
        只有"当前这一次执行"才有资格收尾。若某次执行超时被判定为僵尸、
        工单被别人接管后它才慢吞吞地结束，这条 UPDATE 会因为 run_id 不匹配而
        影响 0 行 —— 既不会误伤新执行，也能让调用方知道"我已经不是持锁者了"。

    返回：受影响行数（1 = 正常收尾，0 = 执行权已经不在自己手上）。
    """
    statement = (
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.agent_status.in_(expected),
            Ticket.agent_run_id == agent_run_id,
        )
        .values(agent_status=next_status, agent_run_id=None)
    )
    return db.execute(statement).rowcount or 0


def settle_agent_execution(
    db: Session,
    ticket_id: int,
    *,
    agent_run_id: int | None,
    final_status: str,
    releasable: tuple[str, ...],
) -> int:
    """把执行权落到终态（IDLE 或 WAITING_HUMAN），比 release 多一个"允许的旧状态集合"。

    `releasable` 由 Service 根据本次结局决定：
        正常跑完 / 失败 / 驳回 → 允许从 (RUNNING,) 收尾成 IDLE
        停在人工确认点        → 允许从 (RUNNING,) 收尾成 WAITING_HUMAN
        人工审批后的恢复执行   → 允许从 (RUNNING, WAITING_HUMAN) 收尾（恢复失败要退回等待态）

    **收尾到 IDLE 与收尾到 WAITING_HUMAN，对 agent_run_id 的处理不同**：

        落到 IDLE          → 清空 agent_run_id（这次执行结束了，工单空闲，可以被重新处理）
        落到 WAITING_HUMAN → **保留 agent_run_id**（这次执行没结束，它正停在人工确认点）

    为什么等待人工时必须留着 run_id：
        1. 它是"工单此刻被哪一次执行占着"的唯一凭据。客服**接管**要做的是一条
           带归属校验的条件 UPDATE（`WHERE agent_status='WAITING_HUMAN' AND agent_run_id=?`）；
           如果这里把 run_id 清成 NULL，那条 UPDATE 永远匹配不到任何行，
           接管会 100% 失败（报"状态已变化"）—— 这不是并发冲突，是状态没记全。
        2. 恢复执行必须恢复**原来那一次**的 thread_id：run_id 留着才能校验
           "客服批准时工单占着的还是它"，避免批准打到别人的新执行上。
        3. 排查问题时，"这张工单现在等的是哪一次执行"应该能直接查出来。
    """
    values: dict = {"agent_status": final_status}
    if final_status == AgentStatus.WAITING_HUMAN:
        values["agent_run_id"] = agent_run_id
    else:
        values["agent_run_id"] = None

    statement = (
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.agent_status.in_(releasable),
            Ticket.agent_run_id == agent_run_id,
        )
        .values(**values)
    )
    return db.execute(statement).rowcount or 0


def get_agent_state(db: Session, ticket_id: int) -> dict | None:
    """只读当前执行权状态（抢占失败时用来告诉调用方"现在是谁在跑"）。"""
    row = db.execute(
        select(Ticket.agent_status, Ticket.agent_run_id).where(Ticket.id == ticket_id)
    ).first()
    if row is None:
        return None
    return {"agent_status": row[0], "agent_run_id": row[1]}


def is_agent_busy(db: Session, ticket_id: int) -> bool:
    """这张工单此刻是不是被 Agent 占着（RUNNING 或 WAITING_HUMAN）。

    注意它只用于**展示/判断**，不用于互斥 ——
    "先调它、再决定要不要抢"就是竞态。互斥永远靠 acquire_agent_execution 的原子 UPDATE。
    """
    state = get_agent_state(db, ticket_id)
    return bool(state and state["agent_status"] in AgentStatus.OCCUPIED)


# ======================================================================
# 工单消息
# ======================================================================
# 注意这里**没有**"发消息"这种业务方法，只有落库：
# 四类发送者（USER / AGENT / CUSTOMER_SERVICE / SYSTEM）的发言都进同一张表，
# 这样用户和客服看到的是同一条对话流（v2.md §20：不单独做 Chat 系统）。
# "谁能在这张工单里发言"由 Service 校验，仓储不判断权限。
def add_message(
    db: Session,
    *,
    ticket_id: int,
    sender_type: str,
    content: str,
    sender_id: int | None = None,
    dedup_key: str | None = None,
) -> dict:
    """追加一条工单消息。

    sender_id 对 AGENT 是 None（Agent 不是一个 user 账号），
    对 SYSTEM 则是触发这条系统消息的操作人（例如接管工单的客服）。
    """
    message, _ = insert_once(db, TicketMessage, dedup_key=dedup_key,
                            ticket_id=ticket_id, sender_type=sender_type, content=content, sender_id=sender_id)
    return message.to_dict()


def has_message_key(db: Session, dedup_key: str) -> bool:
    """调用方持有工单锁，判断同一次执行是否已原子结单。"""
    return db.scalar(select(TicketMessage.id).where(TicketMessage.dedup_key == dedup_key)) is not None


def list_messages(db: Session, ticket_id: int, *, limit: int = 200) -> list[dict]:
    """整条对话流（按 id 正序，也就是时间正序）—— 工单详情页正文用。

    limit 默认 200：工单对话是给人读的，超出部分前端也不该一次全渲染。
    """
    rows = db.scalars(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.id.asc())
        .limit(limit)
    )
    return [message.to_dict() for message in rows]


def list_recent_messages(db: Session, ticket_id: int, *, limit: int = 20) -> list[dict]:
    """Agent 上下文用：最近 N 条消息（按时间正序返回）。"""
    rows = list(
        db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.id.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return [message.to_dict() for message in rows]
