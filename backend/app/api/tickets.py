"""工单接口（v2.md §45 Ticket / HITL）。

用户侧：创建工单、我的工单、详情、发言、重新打开、确认解决
客服侧：在 customer_service.py（列表/接管/审批等）

Agent 相关接口（process / stream / approve / reject）由 agent 模块提供，
本文件先实现工单本身的业务接口。
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request, status, UploadFile, File
from fastapi.responses import StreamingResponse

from app.api import mappers
from app.core.dependencies import get_current_user, require_roles
from app.core.logging import get_logger
from app.models.ticket_message import SenderType
from app.models.user import Role
from app.schemas.agent import (
    AgentApprovalRequest,
    AgentProcessResponse,
    AgentRejectRequest,
)
from app.schemas.ticket import (
    HumanRequest,
    MessageReadRequest,
    TicketCloseRequest,
    TicketCreate,
    TicketDetail,
    TicketListResponse,
    TicketMessageCreate,
    TicketMessageInfo,
    TicketResolveRequest,
    TicketSummary,
)
from app.services import ticket_service, agent_task_service

logger = get_logger(__name__)

router = APIRouter(prefix="/tickets", tags=["tickets"])

# SSE 心跳间隔（秒）
SSE_HEARTBEAT_SECONDS = 15


@router.post("", response_model=TicketDetail, status_code=status.HTTP_201_CREATED, summary="创建工单")
def create_ticket(
    payload: TicketCreate,
    current_user: dict = Depends(get_current_user),
) -> TicketDetail:
    """创建工单。带 order_id 时会校验"订单存在且属于当前用户"（v2.md §18.2）。"""
    ticket = ticket_service.create_ticket(
        user_id=current_user["id"],
        content=payload.content,
        order_id=payload.order_id,
        after_sale_id=payload.after_sale_id,
        type=payload.type,
        title=payload.title,
    )
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.get("", response_model=TicketListResponse, summary="我的工单")
def list_my_tickets(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ticket_status: str | None = Query(default=None, alias="status"),
    current_user: dict = Depends(get_current_user),
) -> TicketListResponse:
    result = ticket_service.list_my_tickets(
        user_id=current_user["id"], limit=limit, offset=offset, status=ticket_status
    )
    return TicketListResponse(
        items=[TicketSummary(**mappers.ticket_summary(t)) for t in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/unread-summary", summary="当前账号未读工单消息汇总")
def unread_summary(current_user: dict = Depends(get_current_user)) -> dict:
    from app.services.message_read_service import summary
    return summary(current_user)


@router.post("/{ticket_id}/read", summary="标记已经显示的消息为已读")
def mark_messages_read(ticket_id: int, payload: MessageReadRequest, current_user: dict = Depends(get_current_user)) -> dict:
    from app.services.message_read_service import mark_read
    return mark_read(ticket_id=ticket_id, viewer=current_user, last_message_id=payload.last_message_id)


@router.post("/{ticket_id}/attachments", response_model=TicketMessageInfo, status_code=201, summary="上传工单图片")
async def upload_attachment(ticket_id: int, file: UploadFile = File(...), current_user: dict = Depends(get_current_user)) -> TicketMessageInfo:
    from app.core.config import settings
    from app.services.attachment_service import upload_image
    try:
        data = await file.read(settings.oss_max_upload_bytes + 1)
        message = await asyncio.to_thread(upload_image, ticket_id=ticket_id, viewer=current_user,
            filename=file.filename or "图片", declared_type=file.content_type or "", data=data)
        return TicketMessageInfo(**mappers.message_info(message))
    finally:
        await file.close()


@router.get("/{ticket_id}/attachments/{attachment_id}/url", summary="鉴权后生成图片短期查看链接")
def attachment_url(ticket_id: int, attachment_id: int, current_user: dict = Depends(get_current_user)) -> dict:
    from app.services.attachment_service import image_url
    return image_url(ticket_id=ticket_id, attachment_id=attachment_id, viewer=current_user)


@router.get("/{ticket_id}", response_model=TicketDetail, summary="工单详情")
def get_ticket(
    ticket_id: int,
    current_user: dict = Depends(get_current_user),
) -> TicketDetail:
    """工单详情：含消息、Agent 分析结果、Agent 业务事件、历史执行记录。"""
    ticket = ticket_service.get_ticket_detail(ticket_id, user_id=current_user["id"])
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post("/{ticket_id}/messages", response_model=TicketMessageInfo, summary="发送消息")
def send_message(
    ticket_id: int,
    payload: TicketMessageCreate,
    current_user: dict = Depends(get_current_user),
) -> TicketMessageInfo:
    """用户发言。已解决/已关闭的工单会自动重新打开（用户可以继续沟通）。"""
    message = ticket_service.add_message(
        ticket_id=ticket_id,
        content=payload.content,
        sender_type=SenderType.USER,
        sender_id=current_user["id"],
        user_id=current_user["id"],
    )
    return TicketMessageInfo(**mappers.message_info(message))


@router.post("/{ticket_id}/reopen", response_model=TicketDetail, summary="重新打开工单")
def reopen_ticket(
    ticket_id: int, current_user: dict = Depends(get_current_user)
) -> TicketDetail:
    """用户对结果不满意 → 工单回到处理中。"""
    ticket = ticket_service.reopen_ticket(ticket_id=ticket_id, user_id=current_user["id"])
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post("/{ticket_id}/request-human", response_model=TicketDetail, summary="用户请求人工客服")
def request_human(ticket_id: int, payload: HumanRequest, current_user: dict = Depends(get_current_user)) -> TicketDetail:
    """运行中只申请安全交接，未运行或已暂停的任务立即进入人工待办。"""
    from app.services import handoff_service
    ticket = handoff_service.request_human(ticket_id=ticket_id, user_id=current_user["id"], reason=payload.reason)
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post("/{ticket_id}/return-to-agent", response_model=TicketDetail, summary="负责客服明确交回 Agent")
def return_to_agent(ticket_id: int, current_user: dict = Depends(require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN))) -> TicketDetail:
    """只解除人工模式；再次启动将创建新任务，不恢复已交接的旧图。"""
    from app.services import handoff_service
    ticket = handoff_service.return_to_agent(ticket_id=ticket_id, operator_id=current_user["id"])
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post(
    "/{ticket_id}/resolve",
    response_model=TicketDetail,
    summary="标记已解决（客服/管理员）",
)
def resolve_ticket(
    ticket_id: int,
    payload: TicketResolveRequest,
    current_user: dict = Depends(require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN)),
) -> TicketDetail:
    ticket = ticket_service.resolve_ticket(
        ticket_id=ticket_id, solution=payload.solution, operator_id=current_user["id"]
    )
    return TicketDetail(**mappers.ticket_detail(ticket))


@router.post("/{ticket_id}/close", response_model=TicketDetail, summary="关闭工单")
def close_ticket(
    ticket_id: int,
    payload: TicketCloseRequest | None = None,
    current_user: dict = Depends(get_current_user),
) -> TicketDetail:
    """关闭工单：用户确认解决，或客服结单。

    权限：普通用户只能关**自己的**工单（Service 层按 user_id 校验归属），
    客服/管理员可以关任何工单（他们本来就要处理全量工单）。
    """
    viewer_id = _viewer_id(current_user)
    ticket = ticket_service.close_ticket(
        ticket_id=ticket_id,
        comment=payload.comment if payload else None,
        operator_id=current_user["id"],
        user_id=viewer_id,
    )
    return TicketDetail(**mappers.ticket_detail(ticket))


# ======================================================================
# Agent：处理 / 进度（SSE）/ 人工审批（HITL）
# ======================================================================
@router.post("/{ticket_id}/process", status_code=202, response_model=AgentProcessResponse, summary="启动 Agent 处理")
async def process_ticket(
    ticket_id: int,
    current_user: dict = Depends(get_current_user),
) -> AgentProcessResponse:
    """提交持久化任务并立即返回 202；已有任务则复用，由独立 worker 执行。"""
    viewer_id = _viewer_id(current_user)
    ticket = ticket_service.get_ticket_raw(ticket_id, user_id=viewer_id)
    result = await asyncio.to_thread(agent_task_service.submit_agent_run,
        ticket_id=ticket_id, user_id=ticket["user_id"])
    run = result["agent_run"] or {}
    agent_state = ticket_service.get_agent_state(ticket_id) or {}
    return AgentProcessResponse(
        ticket_id=ticket_id,
        agent_run_id=run.get("id", 0),
        thread_id=run.get("thread_id", "") or "",
        status=result["status"],
        need_human=result["need_human"],
        decision=run.get("decision"),
        result=run.get("result"),
        agent_status=agent_state.get("agent_status"),
        used_existing_run=bool(result.get("reused")),
        message=result.get("message"),
    )


@router.get("/{ticket_id}/stream", summary="Agent 处理进度（SSE）")
async def stream_ticket(
    ticket_id: int,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """以 SSE 推送这张工单的 Agent 业务事件。

        id: 7
        event: event
        data: {"type":"event","event_type":"QUERY_ORDER","message":"正在查询订单信息","seq":7}

        event: done
        data: {"type":"done","status":"COMPLETED","message":"Agent 处理完成","seq":9}

    三个细节：
        1. 建流时先**回放**历史事件 → 晚连上/断线重连都不漏进度（v2.md §46）；
        2. 每条事件都带 `id: <seq>`；重连时前端带 `Last-Event-ID` 头，
           就只回放它没收到过的那一段 —— 断线期间产生的步骤不会丢，也不会重复；
        3. 空闲每 15 秒发一次心跳注释行，避免被代理掐断。

    SSE 只负责"实时通知"：事件本身早已落库（agent_run_events），
    所以就算流断了、浏览器关了，客服刷新页面照样能看到完整过程。
    """
    ticket_service.get_ticket_raw(ticket_id, user_id=_viewer_id(current_user))
    resume_from = _parse_last_event_id(last_event_id)

    async def event_generator() -> AsyncIterator[str]:
        # 独立 worker 与 API 不共享内存；持久化事件 ID 是跨进程重连游标。
        cursor = resume_from or 0
        heartbeat_ticks = 0
        while not await request.is_disconnected():
            events, run = await asyncio.to_thread(agent_task_service.stream_snapshot, ticket_id, cursor)
            for event in events:
                cursor = event["event_id"]
                yield _sse_format(event)
            if len(events) == 200:
                continue  # 先回放完所有分页，再判断终态。
            if run and run["status"] in ("COMPLETED", "FAILED", "REJECTED", "WAITING_HUMAN"):
                yield _sse_format({"type": "done", "status": run["status"],
                    "agent_run_id": run["id"], "ticket_id": ticket_id,
                    "message": "任务状态已更新，请刷新详情"})
                return
            heartbeat_ticks += 1
            if heartbeat_ticks >= SSE_HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                heartbeat_ticks = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _parse_last_event_id(raw: str | None) -> int | None:
    """解析 `Last-Event-ID` 请求头。

    浏览器自动重连时会把最后一个 `id:` 原样送回来；前端手动重连也是同一个头。
    值不合法（比如被代理改坏）时当作"首次连接"处理 —— 宁可多重放一遍，
    也不要因为一个脏头把整条进度流变成空的。
    """
    if raw is None:
        return None
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("Last-Event-ID 不是合法整数，按首次连接处理：%r", raw)
        return None


def _remember(seen: set[int], event: dict) -> bool:
    """记下这条事件的 seq；返回 False 表示之前已经发过了（应当跳过）。"""
    seq = event.get("seq")
    if seq is None:
        return True
    seq = int(seq)
    if seq in seen:
        return False
    seen.add(seq)
    return True


def _sse_format(event: dict) -> str:
    """把一条事件编成 SSE 帧。

    `id:` 字段是协议规定的位置 —— 浏览器 EventSource 会自动记住它，
    断线重连时自动放进 `Last-Event-ID` 头。前端用 fetch 手写解析时，
    要自己把 id 存下来并在重连时带上（见 frontend/src/api/tickets.ts）。
    """
    event_type = event.get("type", "message")
    lines = []
    if event.get("seq") is not None:
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def _viewer_id(current_user: dict) -> int | None:
    """客服/管理员可以看任何工单的进度；普通用户只能看自己的。"""
    from app.models.user import Role

    return None if current_user["role"] in Role.STAFF else current_user["id"]


@router.post("/{ticket_id}/approve", response_model=TicketDetail, summary="人工批准（HITL）")
async def approve_ticket(
    ticket_id: int,
    payload: AgentApprovalRequest,
    current_user: dict = Depends(require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN)),
) -> TicketDetail:
    """客服批准 Agent 的处理方案 → **恢复被中断的图**继续执行（例如真正执行退款）。"""
    decision = await asyncio.to_thread(agent_task_service.submit_approval,
        ticket_id=ticket_id,
        operator_id=current_user["id"],
        approved=True,
        comment=payload.comment, expected_run_id=payload.expected_run_id, review_round=payload.review_round,
    )
    if decision.get("review_changed"):
        agent_task_service.conflict("订单状态已变化，请刷新后重新审核")
    return TicketDetail(**mappers.ticket_detail(ticket_service.get_ticket_detail(ticket_id)))


@router.post("/{ticket_id}/reject", response_model=TicketDetail, summary="人工驳回（HITL）")
async def reject_ticket(
    ticket_id: int,
    payload: AgentRejectRequest,
    current_user: dict = Depends(require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN)),
) -> TicketDetail:
    """客服驳回 → 结束本次 Agent 流程，工单转客服人工处理。"""
    decision = await asyncio.to_thread(agent_task_service.submit_approval,
        ticket_id=ticket_id,
        operator_id=current_user["id"],
        approved=False,
        comment=payload.reason, expected_run_id=payload.expected_run_id, review_round=payload.review_round,
    )
    if decision.get("review_changed"):
        agent_task_service.conflict("订单状态已变化，请刷新后重新审核")
    return TicketDetail(**mappers.ticket_detail(ticket_service.get_ticket_detail(ticket_id)))


@router.post("/{ticket_id}/takeover", response_model=TicketDetail, summary="人工接管（HITL）")
def takeover_ticket(
    ticket_id: int,
    payload: TicketCloseRequest | None = None,
    current_user: dict = Depends(require_roles(Role.CUSTOMER_SERVICE, Role.ADMIN)),
) -> TicketDetail:
    """客服接管工单：终止 Agent 执行 + 把工单领到自己名下，后续全部人工处理。

    和"驳回"的区别：驳回只是否掉 Agent 的方案，工单状态留在处理中、归属仍为空；
    接管明确表达"这件事我来"，所以同时完成**终止 Agent + 领取归属**，
    工作台上立刻能看出是谁在处理。

    幂等/并发：`tickets.agent_status` 的条件更新保证连点两次只会接管一次
    （第二次返回 409 TAKEOVER_CONFLICT）。
    """
    ticket = ticket_service.takeover_ticket(
        ticket_id=ticket_id,
        operator_id=current_user["id"],
        comment=payload.comment if payload else None,
    )
    return TicketDetail(**mappers.ticket_detail(ticket))
