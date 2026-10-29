"""测试专用接口：只在 `AGENT_STUB_LLM=1`（并发验收实例）上挂载。

存在两个理由，都属于"正常流程测不出来的东西"：

1. **登记工单号**：确定性 LLM 桩按 `ticket_id` 决定行为（某张工单走 HITL、
   某张工单第一次调用失败……），而工单 id 由 MySQL 自增决定，测试开始前不知道。
2. **注入节点故障**：要验证"断点续跑"，必须让图**跑到一半**炸掉
   （LLM 桩只能让"模型调用"失败，做不到"某个节点失败"）。
   这里可以在运行中给指定工单挂一个"首次调用必失败"的节点包装。

绝不注册到正常实例（见 app/main.py 的 lifespan）：
    - 它没有任何鉴权；
    - 它改的是测试桩的内部状态，对真实业务毫无意义。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agent import stub_llm
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/__test__", tags=["__test__"])

#: kind → 桩里对应的名单
_REGISTRY = {
    "human": "human_tickets",
    "fail": "fail_tickets",
    "fatal": "fatal_tickets",
}

#: 节点名 → (模块名, 属性名, 图里的名字)。
#: 这些节点可以被包装成"对某张工单首次调用必失败"。
#: 注意存的是**节点模块**（app.agent.nodes.xxx），因为 graph.py 每次编译时
#: 都会重新从节点模块取函数（见 graph.NODE_FUNCTIONS）。
_PATCHABLE_NODES = {
    "retrieve_knowledge": ("app.agent.nodes.retrieve", "retrieve_knowledge"),
    "generate_result": ("app.agent.nodes.result", "generate_result"),
    "analyze_ticket": ("app.agent.nodes.analyze", "analyze_ticket"),
    "execute_tools": ("app.agent.nodes.tools", "execute_tools"),
}

#: 节点名 → 未被包装的原始函数（包装时存档，恢复时取回）
_ORIGINALS: dict[str, object] = {}


class StubTicketRequest(BaseModel):
    kind: str = Field(..., description="human / fail / fatal")
    ticket_id: int = Field(..., gt=0)


class FailNodeRequest(BaseModel):
    node: str = Field(..., description="retrieve_knowledge / generate_result / analyze_ticket")
    ticket_id: int = Field(..., gt=0)
    message: str = Field(default="Request timed out after 90s（节点故障注入，可恢复）")


@router.post("/stub-tickets", summary="把工单登记进 LLM 桩的行为名单（仅测试实例）")
def register_stub_ticket(payload: StubTicketRequest) -> dict:
    attribute = _REGISTRY.get(payload.kind)
    if attribute is None:
        raise HTTPException(status_code=400, detail=f"未知的 kind：{payload.kind}")

    # 桩实例是"每次绑定 schema 时新建"，但名单集合由 build_stub_factory 闭包持有，
    # 是同一批 set 对象 —— 原地 add 就能影响到后续所有桩实例。
    target = stub_llm.ACTIVE_TICKETS
    if target is None or attribute not in target:
        raise HTTPException(status_code=503, detail="LLM 桩未启用")

    target[attribute].add(payload.ticket_id)
    return {"ok": True, "kind": payload.kind, "ticket_id": payload.ticket_id}


@router.post("/fail-node-once", summary="让某个图节点对指定工单的首次调用失败（仅测试实例）")
def fail_node_once(payload: FailNodeRequest) -> dict:
    """验证"断点续跑"用的故障注入。

    做法：把节点模块里的实现换成"对目标工单首次调用就抛可恢复异常"，然后**重新编译图**。

    为什么要重编译：LangGraph 在 compile 时会把节点函数对象固定进图里，
    光改模块属性对已编译的图无效；而 graph.py 每次编译都重新取一次节点函数，
    所以"改属性 + 重编译"能干净地换掉节点实现。
    这等价于"改代码 + 重启服务"—— 而 Checkpoint 存在 SQLite，与进程无关，
    因此重启后断点依然可用，正好也验证了这一点。
    """
    import importlib

    target = _PATCHABLE_NODES.get(payload.node)
    if target is None:
        raise HTTPException(status_code=400, detail=f"不支持注入故障的节点：{payload.node}")

    module_name, attribute = target
    module = importlib.import_module(module_name)
    current = getattr(module, attribute)
    original = _ORIGINALS.get(payload.node)
    if original is None:
        original = current  # 第一次注入：当前实现就是原始的，先存档
        _ORIGINALS[payload.node] = original
    elif current is not original:
        raise HTTPException(
            status_code=409,
            detail=f"节点 {payload.node} 已经注入过故障，请先调用 /__test__/restore-nodes",
        )

    failed: set[int] = set()
    ticket_id = payload.ticket_id
    message = payload.message
    node_name = payload.node

    async def failing_node(state, *args, **kwargs):  # noqa: ANN001, ANN202
        # 只对目标工单生效、且只失败一次：同一实例可能同时在跑别的测试工单
        if state.get("ticket_id") == ticket_id and ticket_id not in failed:
            failed.add(ticket_id)
            logger.warning("故障注入：节点 %s 对 ticket_id=%s 抛异常（仅首次）", node_name, ticket_id)
            raise RuntimeError(message)
        return await original(state, *args, **kwargs)

    # 保留原名：日志与事件里看到的还是同一个节点
    failing_node.__name__ = getattr(original, "__name__", node_name)
    setattr(module, attribute, failing_node)
    _recompile_graph()
    return {"ok": True, "node": node_name, "ticket_id": ticket_id}


@router.post("/restore-nodes", summary="恢复所有被注入故障的节点（仅测试实例）")
def restore_nodes() -> dict:
    import importlib

    restored: list[str] = []
    for node, original in list(_ORIGINALS.items()):
        module_name, attribute = _PATCHABLE_NODES[node]
        setattr(importlib.import_module(module_name), attribute, original)
        restored.append(node)
        _ORIGINALS.pop(node, None)
    if restored:
        _recompile_graph()
    return {"ok": True, "restored": restored}


def _recompile_graph() -> None:
    """用同一个 Checkpointer 重新编译图，让节点包装生效（等价于"改代码后重启服务"）。"""
    from app.agent.graph import build_graph, set_graph
    from app.memory.checkpoint import get_checkpointer

    set_graph(build_graph(get_checkpointer()))
    logger.warning("已用同一 Checkpointer 重新编译 Agent 图（故障注入后生效）")
