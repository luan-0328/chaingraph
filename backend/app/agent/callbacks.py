"""节点级回调：把"此刻正在跑哪个节点"写进 AgentRun（可观测性）。

事件（agent_run_events）记录的是**业务语义**（正在查订单 / 命中了几条规则），
节点回调记录的是**执行位置**（现在卡在 analyze_ticket 还是 execute_action）。
两者互补：一个是"Agent 在想什么"，一个是"程序跑到哪一行了"。

为什么用 LangGraph 回调而不是在每个节点里手写一行：
    节点函数应该只关心业务；执行位置属于框架层信息，交给回调最干净。
    LangGraph 给每个节点 runnable 都设了 run_name = 节点名，
    on_chain_start / on_chain_end 就能拿到它。

写入频率很低（每个节点两次），且每个节点本来就会写事件，属于同一个量级。
写失败绝不影响 Agent 主流程 —— 它只是观测数据。
"""

import asyncio

from langchain_core.callbacks import AsyncCallbackHandler

from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.time import utcnow
from app.repositories import agent_run_repository

logger = get_logger(__name__)

#: 只有这些名字才是"图里的节点"，其余（模型、检索器、工具）不当作节点记录
NODE_NAMES = (
    "analyze_ticket",
    "retrieve_knowledge",
    "execute_tools",
    "generate_result",
    "human_review",
    "execute_action",
)


class NodeTrackingCallback(AsyncCallbackHandler):
    """把节点的进入/离开写进 agent_runs.current_node。

    agent_run_id 由调用方（_invoke_graph）通过 config 传进来：
    同一次执行里的所有节点共用同一个 id。
    """

    def __init__(self, agent_run_id: int) -> None:
        self.agent_run_id = agent_run_id

    # ---- 进入节点 ----
    async def on_chain_start(self, serialized, inputs, *, run_id=None, **kwargs):  # noqa: ANN001, ANN003
        """进入节点：把节点名写进 `agent_runs.current_node`。

        这样"这次执行此刻卡在哪一步"是数据库里一眼可见的事实 ——
        配合 alive_at 心跳，运维能区分"正在跑 LLM"和"进程已经死了"。
        """
        node = self._node_name(kwargs)
        if node is None:
            return
        await asyncio.to_thread(self._write, node)

    # ---- 离开节点（正常结束 / 出错都走这里）----
    async def on_chain_end(self, outputs, *, run_id=None, **kwargs):  # noqa: ANN001, ANN003
        """离开节点：清空 current_node（不跑在节点里时就该是空的）。"""
        node = self._node_name(kwargs)
        if node is None:
            return
        await asyncio.to_thread(self._write, None)

    async def on_chain_error(self, error, *, run_id=None, **kwargs):  # noqa: ANN001, ANN003
        """节点抛错：同样清空 current_node。

        为什么不把错误写进 current_node：那个字段的语义是"正在跑什么"，
        失败原因属于事件流（agent_run_events）和 AgentRun.error ——
        往一个"位置"字段里塞错误信息，会让两个字段的语义都变得不可靠。
        """
        # 节点抛错时把位置清掉：失败原因由事件流记录，current_node 只表达"正在跑什么"
        node = self._node_name(kwargs)
        if node is None:
            return
        await asyncio.to_thread(self._write, None)

    # ---- 内部 ----
    @staticmethod
    def _node_name(kwargs: dict) -> str | None:
        """从回调参数里认出节点名；认不出来（或不是我们关心的节点）返回 None。

        为什么要过滤 `name in NODE_NAMES`：LangGraph 除了图节点，还会为
        内部 chain / 结构化输出包装产生回调。不过滤的话 current_node
        会被刷成 "RunnableSequence" 这种对排查毫无帮助的名字。
        """
        name = kwargs.get("name") or (kwargs.get("metadata") or {}).get("langgraph_node")
        return name if name in NODE_NAMES else None

    def _write(self, node: str | None) -> None:
        """落库（同步阻塞，调用方用 `asyncio.to_thread` 包起来）。

        为什么必须丢到线程里：SQLAlchemy 是同步库，直接在 async 回调里执行
        会把事件循环卡住 —— 一次 Agent 执行要写好几次，累积起来很可观。

        失败只告警：可观测性数据写不进去，绝不该让 Agent 执行本身失败。
        """
        try:
            with session_scope() as db:
                agent_run_repository.set_current_node(
                    db, self.agent_run_id, node, at=utcnow()
                )
        except Exception as exc:  # noqa: BLE001 —— 观测写入失败不能影响执行
            logger.warning("写入 current_node 失败（不影响执行）：%s", exc)
