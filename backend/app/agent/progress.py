"""Agent 处理进度总线（SSE 的数据源）。

一份事件，两个出口：
    1. 落库 agent_run_events（客服工作台回看、审计、刷新页面后仍能看到过程）
    2. 内存总线广播 → GET /api/tickets/{id}/stream 实时推给前端
       —— 前端不用等接口返回，也不用轮询，Agent 走到哪一步就推到哪一步。

关键设计（和 v1 一致，实测有效）：
    - 按 ticket_id 分频道；
    - **保留历史事件**：新订阅者/断线重连会先收到回放，
      解决"前端先 POST 再连流"的竞态，也满足 v2.md §46 的断线恢复要求。
    - **每个事件带单调递增的 seq，并作为 SSE 的 `id:` 下发**：
      浏览器/前端断线重连时可以带 `Last-Event-ID: <seq>`，
      服务端就只回放它没收到过的事件 —— 断线期间产生的步骤不会丢。

局限（明确接受）：
    进程内内存实现，多实例部署时不互通 —— 那时换 Redis Pub/Sub，
    emit_event() 的调用方不用改（seq 也正好可以直接用 Redis Stream 的 ID）。
"""

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from app.core.logging import get_logger

logger = get_logger(__name__)

HISTORY_SIZE = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ProgressBus:
    """按工单分频道的进程内事件总线（SSE 的实时侧）。

    每个工单维护三样东西：

        _history     最近 HISTORY_SIZE 条事件（给晚连上/重连的客户端回放）
        _subscribers 正在听这张工单的队列集合（一个工单可以有多端同时在看：
                     用户端 + 客服端同时打开同一张工单是常见场景）
        _seq         已发到第几号（断线重连的续传游标）

    为什么按工单分频道而不是一条全局流：客服工作台同时开着好几张工单时，
    全局流会让每个连接都收到所有工单的事件，前端要自己过滤 ——
    既浪费带宽，也让"这条事件属于哪张工单"变成前端的责任。
    """

    def __init__(self) -> None:
        self._history: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=HISTORY_SIZE))
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        #: 每个工单下一条事件的流水号（从 1 开始）。
        #: 只用于"断线重连时从哪继续" —— 它是实时通道的游标，不是业务主键；
        #: 真正的审计主键是数据库里的 agent_run_events.id。
        self._seq: dict[int, int] = defaultdict(int)

    def reset(self, ticket_id: int) -> None:
        """开始新一轮处理前清空历史（避免重连时看到上一次的旧进度）。

        seq **不清零**：同一张工单的处理历史在时间上是单调的，
        清零会让"上一轮的 seq=3"和"这一轮的 seq=3"撞车，重连时无法区分。
        """
        self._history.pop(ticket_id, None)

    def publish(self, ticket_id: int, event: dict) -> None:
        """广播一条事件：打上序号与时间 → 存历史 → 推给所有订阅者。

        三件事的顺序不能乱：
            ① 先算 seq（它是这条事件的"重连游标"，必须唯一且单调）
            ② 再进历史（晚了的话，恰好在这中间连上来的订阅者会拿不到它）
            ③ 最后入队推给实时订阅者
        """
        self._seq[ticket_id] += 1
        payload = {
            **event,
            "seq": self._seq[ticket_id],
            "at": event.get("at") or _now_iso(),
        }
        self._history[ticket_id].append(payload)
        for queue in list(self._subscribers.get(ticket_id, ())):
            queue.put_nowait(payload)
        logger.debug(
            "SSE 事件 ticket_id=%s seq=%s %s %s",
            ticket_id,
            payload["seq"],
            payload.get("event_type"),
            payload.get("message"),
        )

    def history_since(self, ticket_id: int, last_event_id: int | None) -> list[dict]:
        """回放历史：`last_event_id` 之后的那些。

        last_event_id=None  → 全部回放（首次连接）
        last_event_id=N     → 只给 seq > N 的（断线重连，不漏也不重）
        """
        history = list(self._history.get(ticket_id, ()))
        if last_event_id is None:
            return history
        return [event for event in history if int(event.get("seq") or 0) > last_event_id]

    def current_seq(self, ticket_id: int) -> int:
        """这张工单已经发到第几号（诊断/测试用：判断"有没有新事件"）。"""
        return self._seq.get(ticket_id, 0)

    @asynccontextmanager
    async def subscribe(
        self, ticket_id: int, *, last_event_id: int | None = None
    ) -> AsyncIterator[tuple[asyncio.Queue, list[dict]]]:
        """订阅：先注册队列、再取历史快照。

        顺序很关键：**先入队再读历史**，否则"注册"和"读历史"之间有窗口，
        恰好落在这个窗口里的事件会既不在历史里、也没进队列 —— 丢事件。
        先注册的话，最坏情况是同一个事件既在回放里、又在队列里，
        而这可以用 seq 去重（重复比丢失好处理得多）。

        `last_event_id` 便于重连时只拿缺失的那段。
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[ticket_id].add(queue)
        try:
            yield queue, self.history_since(ticket_id, last_event_id)
        finally:
            self._subscribers[ticket_id].discard(queue)

    def subscriber_count(self, ticket_id: int) -> int:
        """当前有多少个 SSE 连接在听这张工单（日志诊断用）。

        排查"前端没收到进度"时第一个该看的数：
        是 0 说明流没建起来（多半是连接被代理掐了或前端提前 abort），
        不是 0 才轮到怀疑事件没发出去。
        """
        return len(self._subscribers.get(ticket_id, ()))


progress_bus = ProgressBus()
