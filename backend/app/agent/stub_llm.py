"""确定性 LLM 桩：只服务于**并发验收测试**（AGENT_STUB_LLM=1 时启用）。

为什么需要它
------------
并发控制的验收标准是"同一工单 10 个并发请求只产生 1 次执行、不同工单能同时执行"。
要证明"同时"和"只产生一次"，必须让每次执行的**耗时可控且足够长**：

    真实 LLM   一次调用几百毫秒~几秒，窗口不稳定，"并发"很容易退化成"顺序"
    本桩       固定 sleep N 秒，10 个请求的执行窗口完全重叠，结论才可信

它替换的只是"模型输出"这一层（app/agent/llm.py 的 resolve_structured_llm），
LangGraph、节点、Tool、RAG、HITL、落库、SSE 全都走真实代码路径 ——
所以并发行为（数据库抢占、finally 释放、心跳、恢复原图）被真实地测试到了。

启用方式（只影响测试实例，正常启动完全不加载这个模块）：

    AGENT_STUB_LLM=1
    AGENT_STUB_DELAY_SECONDS=1.5      # 每次"模型调用"的耗时，用来撑开并发窗口
    AGENT_STUB_HUMAN_TICKETS=101      # 这些工单走 HITL：结果要求人工确认
    AGENT_STUB_FAIL_TICKETS=102       # 这些工单的第一次调用抛"可恢复异常"，用于验重试
    AGENT_STUB_FATAL_TICKETS=103      # 这些工单抛"不可恢复异常"，用于验失败不重试
"""

import asyncio
import os

from app.agent.schemas import AgentDecision, AgentResult
from app.core.logging import get_logger

logger = get_logger(__name__)


def _int_set(name: str) -> set[int]:
    """读环境变量里的"工单号名单"（`AGENT_STUB_FAIL_TICKETS=101,102` 这种）。

    逗号/空格都当分隔符，非法项**跳过而不是报错** ——
    测试配置写错一个数字，不该让整个测试实例起不来。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    values: set[int] = set()
    for part in raw.replace(",", " ").split():
        try:
            values.add(int(part))
        except ValueError:
            continue
    return values


def _delay_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("AGENT_STUB_DELAY_SECONDS", "1.0")))
    except ValueError:
        return 1.0


class _RetryableStubError(RuntimeError):
    """模拟 LLM 侧的可恢复故障（超时 / 429 / 连接抖动）。"""


class _FatalStubError(RuntimeError):
    """模拟不可恢复故障（模型鉴权失败等）。"""


class _StubStructuredLlm:
    """一个"结构化输出"桩：按当前工单返回固定的 AgentDecision / AgentResult。

    关键点：**每个实例一份计数**，用来模拟"同一个工单的第 1 次调用失败、第 2 次成功"。
    计数放在类级别（按 ticket_id 分组）让重试语义与真实场景一致。
    """

    #: ticket_id -> 已经调用过几次（跨实例共享，模拟供应商侧的真实状态）
    _calls: dict[int, int] = {}

    def __init__(
        self,
        schema,
        delay: float,
        human_tickets: set[int],
        fail_tickets: set[int],
        fatal_tickets: set[int],
        business_action: str = "",
    ) -> None:
        self.schema = schema
        self.delay = delay
        self.human_tickets = human_tickets
        self.fail_tickets = fail_tickets
        self.fatal_tickets = fatal_tickets
        self.business_action = business_action

    async def ainvoke(self, messages):  # noqa: ANN001, ANN201
        """假装调了一次模型：先 sleep，再按工单号决定"正常/失败/要人工"。

        顺序刻意是"先 sleep 再判断"：故障注入也要占据那段耗时窗口，
        否则"可恢复异常立刻返回"会让重试测试跑得比并发测试还快，
        测不出"重试期间别人能不能并发"这件事。
        """
        ticket_id = _ticket_id_from_messages(messages)
        if self.delay:
            # 用 sleep 撑开并发窗口：这段时间里其它工单的请求应当能同时跑
            await asyncio.sleep(self.delay)

        attempts = _StubStructuredLlm._calls.get(ticket_id, 0) + 1
        _StubStructuredLlm._calls[ticket_id] = attempts

        if ticket_id in self.fatal_tickets and attempts == 1:
            # 不可恢复：业务/鉴权类错误，不该被重试
            raise _FatalStubError("模型鉴权失败：invalid api key（不可恢复，测试用）")
        if ticket_id in self.fail_tickets and attempts == 1:
            # 可恢复：第一次抛超时，第二次正常返回 —— 验证"只对可恢复异常重试"
            raise _RetryableStubError("Request timed out after 90s（可恢复，测试用）")

        if self.schema is AgentDecision:
            return self._decision(ticket_id)
        if self.schema is AgentResult:
            return self._result(ticket_id)
        raise RuntimeError(f"桩不认识的结构化输出类型：{self.schema}")

    # ---- 固定输出 ----
    def _decision(self, ticket_id: int) -> AgentDecision:
        """固定的 AgentDecision。

        `need_retrieval=True` 是刻意的：让每次执行都真的走一遍 RAG，
        于是"检索节点"也被并发/断点续跑测试覆盖到（而不用外部 Embedding 服务）。
        三个 need_*_query 全 False：并发测试只想测调度，不想连带给工具节点。
        """
        # need_retrieval=True 让图走一次真实 RAG（本地向量器，不需要外部服务）
        return AgentDecision(
            request_kind="ACTION" if self.business_action else "CONSULTATION",
            issue_type="REFUND",
            summary=f"[stub] 工单 {ticket_id} 申请退款",
            need_retrieval=True,
            need_order_query=False,
            need_logistics_query=False,
            need_after_sale_query=False,
            can_auto_process=True,
            need_human=ticket_id in self.human_tickets,
        )

    def _result(self, ticket_id: int) -> AgentResult:
        """固定的 AgentResult；`need_human` 由工单号决定（用来驱动 HITL 测试）。

        `business_action` 来自环境变量（实例级）而不是按工单切换：
        真实模型也不可能"对这张单退款、对那张单不退"由外部开关决定 ——
        想让某个实例真的动钱，就得单独起一个实例（见 tests/conftest.py 的 chain_server）。
        """
        need_human = ticket_id in self.human_tickets
        return AgentResult(
            resolution_kind="ACTION" if self.business_action else "ANSWER",
            issue_type="REFUND",
            summary=f"[stub] 工单 {ticket_id} 的退款申请",
            analysis="[stub] 固定分析结论（并发测试不需要真实推理）",
            solution=f"[stub] 工单 {ticket_id} 的处理建议：按规则处理",
            references=[],
            tool_results=[],
            # business_action 决定"要不要真的动业务"：
            #   默认空串 —— 并发测试只关心调度，不碰资金流水、不污染演示数据；
            #   设成 apply_refund —— 端到端链路测试要验证"Agent 真的把钱退了"，
            #   这时请求会走完 Tool → RefundService → Repository → MySQL 的完整路径。
            business_action=self.business_action,
            need_human=need_human,
        )


def _ticket_id_from_messages(messages) -> int:  # noqa: ANN001
    """从 prompt 里抠出 ticket_id（节点把工单号写在 user prompt 里）。

    桩必须知道"现在在跑哪张工单"，才能做到"指定工单走 HITL / 指定工单失败"。
    analyze 节点的 prompt 里是「工单编号：N」，result 节点同样带这一行（见 nodes/result.py）。
    """
    import re

    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            continue
        match = re.search(r"ticket_id=(\d+)", content) or re.search(r"工单编号：(\d+)", content)
        if match:
            return int(match.group(1))
    return -1


def build_stub_factory():  # noqa: ANN202
    """返回一个可以塞进 llm.set_structured_llm_factory(...) 的工厂。"""
    delay = _delay_seconds()
    human_tickets = _int_set("AGENT_STUB_HUMAN_TICKETS")
    fail_tickets = _int_set("AGENT_STUB_FAIL_TICKETS")
    fatal_tickets = _int_set("AGENT_STUB_FATAL_TICKETS")
    # 空串 = 只给结论、不动业务；apply_refund = 走完整的退款链路（端到端测试用）
    business_action = os.getenv("AGENT_STUB_BUSINESS_ACTION", "").strip()

    # 把三份名单挂到模块上，让测试脚本可以通过 /__test__/stub-tickets
    # 在**运行中**把新建的工单号加进来（工单 id 由 MySQL 自增，测试前不知道）。
    # 注意是原地修改同一批 set —— 桩实例读的就是它们。
    global ACTIVE_TICKETS
    ACTIVE_TICKETS = {
        "human_tickets": human_tickets,
        "fail_tickets": fail_tickets,
        "fatal_tickets": fatal_tickets,
    }

    logger.warning(
        "启用确定性 LLM 桩：delay=%ss human=%s fail-once=%s fatal=%s business_action=%s",
        delay,
        sorted(human_tickets) or "-",
        sorted(fail_tickets) or "-",
        sorted(fatal_tickets) or "-",
        business_action or "（不动业务）",
    )

    def factory(schema):  # noqa: ANN001, ANN202
        """按目标 schema 造一个桩实例（`llm.resolve_structured_llm` 每次调用它）。

        三份名单是**闭包共享的同一批 set 对象** —— 所以测试接口
        `/api/__test__/stub-tickets` 能通过原地 `add(ticket_id)`
        影响之后所有新建的桩实例（工单 id 是自增的，测试开始前不知道）。
        """
        return _StubStructuredLlm(
            schema, delay, human_tickets, fail_tickets, fatal_tickets, business_action
        )

    return factory


#: 当前生效的三份名单（测试接口用它在运行中登记工单号）；未启用桩时为 None
ACTIVE_TICKETS: dict[str, set[int]] | None = None
