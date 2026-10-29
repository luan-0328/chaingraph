"""Agent 执行记录 + 事件数据访问。

这里只做"读写 AgentRun"，**不做互斥**：
    互斥由 tickets.agent_status 的原子 UPDATE 保证（见 ticket_repository），
    本文件提供的是"查询某次执行 / 某张工单当前活跃的执行"这类支撑查询。

一条 AgentRun = 一次执行（用户点一次"开始处理"、或客服批准后恢复一次）。
系统**内部**的重试（可恢复异常）不新建 AgentRun，而是更新同一条：
thread_id 上有唯一约束，断点续跑复用的就是同一个 LangGraph 上下文。
"""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.agent_event import AgentRunEvent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.ticket import Ticket
from app.repositories.idempotency import insert_once


def create_run(
    db: Session,
    *,
    ticket_id: int,
    user_id: int,
    thread_id: str,
    status: str = AgentRunStatus.PENDING,
) -> dict:
    """新建一次 Agent 执行（一次"开始处理"= 一条记录）。

    `thread_id` 是这条记录的身份证，上面有唯一约束：
    它同时是 LangGraph 的 Checkpoint 键，所以一次执行的图上下文只能对应一条记录 ——
    这道约束是"并发下不会创建两条活跃 AgentRun"的最后防线
    （第一道是 tickets.agent_status 的原子抢占）。

    **本函数不做互斥判断**：调用方必须已经抢到执行权（见 ticket_service.acquire_agent_execution），
    否则就是"先查再建"的竞态写法。
    """
    run = AgentRun(ticket_id=ticket_id, user_id=user_id, thread_id=thread_id, status=status)
    db.add(run)
    db.flush()
    db.refresh(run)
    return run.to_dict()


def mark_retrying(
    db: Session,
    run_id: int,
    *,
    attempt: int,
    error: str | None = None,
    resumed: bool = False,
) -> dict | None:
    """本次执行内部的一次重试尝试：刷新重试次数 / 心跳，必要时标记"用过断点续跑"。

    为什么重试不新建 AgentRun（而是更新同一条）：
        thread_id 上是唯一约束 —— 一次执行的 LangGraph 上下文只能对应一条执行记录。
        断点续跑复用同一个 thread_id，所以它必然是同一条 AgentRun 的继续。
        外部"重新发起处理"（用户再点一次）才是新的一次执行，那会生成新的 thread_id。

    这里不把 error 清空：失败原因留着，续跑真的成功时由 mark_finished 一并清掉。
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        return None
    run.retry_count = attempt
    run.alive_at = utcnow()
    if resumed:
        run.resumed = True
    if error:
        run.error = error[:1000]
    db.flush()
    db.refresh(run)
    return run.to_dict()


def get_run(db: Session, run_id: int) -> dict | None:
    """按主键取一条执行记录（字典形式）。"""
    run = db.get(AgentRun, run_id)
    return run.to_dict() if run else None


def get_run_model(db: Session, run_id: int) -> AgentRun | None:
    """按主键取执行记录的 **ORM 对象**（mark_finished 等改字段的场景用）。"""
    return db.get(AgentRun, run_id)


def get_latest_run(db: Session, ticket_id: int) -> dict | None:
    """工单当前（最近一次）Agent 执行 —— 客服工作台默认看这条。"""
    run = db.scalar(
        select(AgentRun).where(AgentRun.ticket_id == ticket_id).order_by(AgentRun.id.desc())
    )
    return run.to_dict() if run else None


def list_runs(db: Session, ticket_id: int) -> list[dict]:
    """历史执行记录（重试会产生多条，全部保留，见 v2.md §66）。"""
    rows = db.scalars(
        select(AgentRun).where(AgentRun.ticket_id == ticket_id).order_by(AgentRun.id.desc())
    )
    return [run.to_dict() for run in rows]


def find_waiting_run(db: Session, ticket_id: int) -> dict | None:
    """找到**工单此刻真的在等**的那次执行（approve/reject 要用它的 thread_id 恢复图）。

    和 find_active_run 同一个道理：必须同时看 run.status 和工单的指针。
    历史上"停过人工、后来又被释放/接管"的 run 会留在库里（状态是 WAITING_HUMAN），
    如果只按状态查，就会拿一条没人等的 run 去恢复图 —— 那是张冠李戴。
    """
    run = db.scalar(
        select(AgentRun)
        .join(Ticket, Ticket.id == AgentRun.ticket_id)
        .where(
            AgentRun.ticket_id == ticket_id,
            AgentRun.status == AgentRunStatus.WAITING_HUMAN,
            Ticket.agent_run_id == AgentRun.id,
        )
        .order_by(AgentRun.id.desc())
    )
    return run.to_dict() if run else None


def find_active_run(db: Session, ticket_id: int) -> dict | None:
    """工单当前"活跃"的 AgentRun（在跑 / 停在人工确认点）。

    抢占失败时要靠它回答"那现在跑的是哪一次"（返回已有 Run 而不是新建一个）。

    **判据必须同时看 run 的状态和工单的指向**（`tickets.agent_run_id == run.id`）。
    为什么不能只看 run.status —— 这是一个真实踩过的坑：

        AgentRun 停在 WAITING_HUMAN 时，如果后来又走到"释放/接管"路径，
        `settle_agent_execution` 会把工单的 agent_status 收回 IDLE 并把指针清空，
        但**不会**去改 AgentRun.status（那条 run 的历史是只增不改的）。
        于是库里留下一条"状态是 WAITING_HUMAN、但工单早就不等它了"的记录。
        只按 status 查，就会把它当成"还在等人工"，导致：
            · 客服重复接管被判成"状态已变化"，接管 100% 失败；
            · 用户重新点"处理"时 _reuse_active_run 认为还在等人工，永远起不来新执行 ——
              一张工单从此彻底卡死。

    终态（COMPLETED / FAILED / REJECTED）同样不算活跃：失败后允许重新发起，
    那是一次新的 AgentRun（v2.md §66：历史只增不改）。
    """
    run = db.scalar(
        select(AgentRun)
        .join(Ticket, Ticket.id == AgentRun.ticket_id)
        .where(
            AgentRun.ticket_id == ticket_id,
            AgentRun.status.in_(AgentRunStatus.ACTIVE),
            # 工单此刻必须真的指着它 —— 指针空掉就说明这次执行已经不是"当前"了
            Ticket.agent_run_id == AgentRun.id,
        )
        .order_by(AgentRun.id.desc())
    )
    return run.to_dict() if run else None


def sum_retry_count(db: Session, ticket_id: int) -> int:
    """这张工单上所有执行的重试次数之和（运维看板 / 验收脚本用）。"""
    return int(db.scalar(select(func.coalesce(func.sum(AgentRun.retry_count), 0)).where(AgentRun.ticket_id == ticket_id)) or 0)


def mark_started(db: Session, run_id: int, started_at: datetime) -> dict | None:
    """PENDING → RUNNING，并写下开始时间。

    **同时把 alive_at 初始化成 started_at**：心跳从这一刻开始计时。
    少了这一步，一条刚创建就崩掉的执行会没有心跳，
    僵尸回收逻辑只能退回用 started_at 兜底（能work，但不够明确）。
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        return None
    run.status = AgentRunStatus.RUNNING
    run.started_at = started_at
    run.alive_at = started_at
    db.flush()
    db.refresh(run)
    return run.to_dict()


def set_current_node(db: Session, run_id: int, node: str | None, *, at: datetime) -> int:
    """记录"此刻正在跑哪个节点"（同时刷新心跳）。

    由 LangGraph 的节点回调写入：进入节点时写节点名，离开时写 None。
    这样"卡在哪一步"是数据库里一眼可见的事实，而不是要去翻日志。
    """
    return (
        db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(current_node=node, alive_at=at)
        ).rowcount
        or 0
    )


def heartbeat(db: Session, run_id: int, at: datetime) -> int:
    """刷新心跳（Agent 每产生一个业务事件就顺带刷一次）。

    用来识别"进程被杀 / 容器重启"留下的僵尸 RUNNING：
    心跳停在很久以前的那次执行，允许被后来的请求接管。
    """
    return (
        db.execute(update(AgentRun).where(AgentRun.id == run_id).values(alive_at=at)).rowcount
        or 0
    )


def mark_finished(
    db: Session,
    run_id: int,
    *,
    status: str,
    finished_at: datetime,
    used_rag: bool | None = None,
    used_tool: bool | None = None,
    need_human: bool | None = None,
    decision: dict | None = None,
    result: dict | None = None,
    error: str | None = None,
    clear_error: bool = False,
    retry_count: int | None = None,
) -> dict | None:
    """收尾一次执行。

    clear_error=True 用于"重试后成功"：把上一次尝试留下的 error 清掉
    （失败详情仍留在 agent_run_events 里，历史不会丢）。
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        return None

    run.status = status
    run.finished_at = finished_at
    if used_rag is not None:
        run.used_rag = used_rag
    if used_tool is not None:
        run.used_tool = used_tool
    if need_human is not None:
        run.need_human = need_human
    if decision is not None:
        run.decision = decision
    if result is not None:
        run.result = result
    if error is not None:
        run.error = error
    if clear_error:
        run.error = None
    if retry_count is not None:
        run.retry_count = retry_count
    # 执行结束就不该再有"当前节点"：留着会让运维误以为还在跑
    run.current_node = None

    db.flush()
    db.refresh(run)
    return run.to_dict()


def add_event(
    db: Session,
    *,
    agent_run_id: int,
    event_type: str,
    message: str,
    dedup_key: str | None = None,
) -> dict:
    """追加一条 Agent 业务事件（Agent 走了哪一步）。

    只记**业务事件**（正在查询订单 / 正在检索规则 / 执行退款 / 等待人工），
    绝不记模型思维链 —— 前端要展示的是"系统做了什么"，不是"模型想了什么"。

    每条事件都用独立短事务写（见 agent/events.py）：一次 Agent 执行可能跑几十秒，
    把整轮执行包进一个长事务会长时间占着连接与行锁。
    """
    event, inserted = insert_once(db, AgentRunEvent, dedup_key=dedup_key,
                                  agent_run_id=agent_run_id, event_type=event_type, message=message)
    return {**event.to_dict(), "inserted": inserted}


def list_events(db: Session, agent_run_id: int) -> list[dict]:
    """某次执行的完整业务事件流（按 id 正序 = 时间顺序）。

    客服工作台的"Agent 执行事件"、以及排查"卡在哪一步"都读它 ——
    它和 SSE 推的是同一份数据，只是这里从库里读（刷新页面也不丢）。
    """
    rows = db.scalars(
        select(AgentRunEvent)
        .where(AgentRunEvent.agent_run_id == agent_run_id)
        .order_by(AgentRunEvent.id.asc())
    )
    return [event.to_dict() for event in rows]


def count_by_status(db: Session) -> dict[str, int]:
    """各状态的执行数量（运维看板用：能一眼看出有没有堆着 FAILED）。

    注意这里**不补零**（没有的状态不出现）—— 调用方是统计展示，
    "没有 FAILED" 显示为缺键比显示 0 更能说明"一个都没有"。
    """
    rows = db.execute(select(AgentRun.status, func.count()).group_by(AgentRun.status)).all()
    return {status: int(count) for status, count in rows}
