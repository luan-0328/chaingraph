"""独立单机 Agent worker：python -m app.worker。

进程锁覆盖整个 Checkpoint 生命周期，旧进程退出后新进程才能恢复同一文件。
本版本不支持跨机器 worker；不能把 SQLite 文件复制到另一台机器后同时运行。
"""
import asyncio
from contextlib import contextmanager
import hashlib
import os
import socket
import threading
import uuid

from langgraph.types import Command

from app.core.config import settings, BASE_DIR
from app.core.database import init_database
from app.core.execution import ExecutionClaim, ExecutionLost, execution_claim
from app.core.logging import get_logger, setup_logging
from app.services import agent_task_service as tasks

logger = get_logger(__name__)


@contextmanager
def single_worker_lock():
    from sqlalchemy.engine import make_url
    url = make_url(settings.database_url)
    identity = f"{url.host}:{url.port or 3306}/{url.database}"
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    # 同一业务库即使误配成不同 Checkpoint 路径，也不能在本机启动两个 worker。
    path = BASE_DIR / "data" / f"agent-worker-{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("同一 Checkpoint 已有 worker；请先停止旧 worker") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def keep_lease(claim, stop: threading.Event):
    while not stop.wait(settings.agent_heartbeat_seconds):
        try:
            tasks.renew_execution_lease(claim)
        except Exception:
            # 无法确认执行权时终止整个 worker，不能只取消协程而留下工具线程继续写库。
            logger.exception("续租失败，worker 立即退出；由重启后的 worker 核实并恢复")
            os._exit(2)


async def execute_agent_run(graph, run: dict):
    from app.services.agent_service import _build_initial_state, RECURSION_LIMIT
    from app.services.ticket_service import get_ticket_raw
    from app.agent.callbacks import NodeTrackingCallback

    config = {"configurable": {"thread_id": run["thread_id"]},
              "recursion_limit": RECURSION_LIMIT, "callbacks": [NodeTrackingCallback(run["id"])]}
    snapshot = await graph.aget_state(config)
    saved = snapshot.values or {}
    interrupted = any(getattr(task, "interrupts", ()) for task in snapshot.tasks)
    if saved:
        if interrupted:
            if run.get("approval_decision"):
                return await graph.ainvoke(Command(resume=run["approval_decision"]), config=config)
            # 崩溃发生在 interrupt 已保存、MySQL 尚未收尾的窗口。
            return {**saved, "__interrupt__": True}
        if not snapshot.next:
            return saved  # 图已完成，只补 MySQL 收尾，不重复动业务。
        return await graph.ainvoke(None, config=config)
    if run.get("approval_decision"):
        raise RuntimeError("审批任务的 Checkpoint 丢失，禁止从头执行，请人工接管")
    if run["execution_epoch"] > 1:
        raise RuntimeError("恢复任务缺少 Checkpoint，禁止猜测执行进度，请人工核实后重新提交")
    ticket = get_ticket_raw(run["ticket_id"])
    initial = _build_initial_state(ticket=ticket, agent_run_id=run["id"],
        thread_id=run["thread_id"], content=ticket["content"])
    return await graph.ainvoke(initial, config=config)


async def main():
    setup_logging()
    if not 0 < settings.agent_heartbeat_seconds < settings.agent_lease_seconds / 2:
        raise RuntimeError("心跳间隔必须大于 0 且小于租约时长的一半")
    if settings.agent_poll_seconds <= 0:
        raise RuntimeError("任务扫描间隔必须大于 0")
    with single_worker_lock():
        if not init_database():
            raise RuntimeError("数据库初始化失败，worker 未启动")
        from app.agent.graph import build_graph, set_graph
        from app.memory.checkpoint import init_checkpointer, close_checkpointer
        from app.rag.vector_store import reset_vector_store
        if settings.agent_stub_llm:
            from app.agent.llm import set_structured_llm_factory
            from app.agent.stub_llm import build_stub_factory
            set_structured_llm_factory(build_stub_factory())
        graph = build_graph(await init_checkpointer())
        set_graph(graph)
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        logger.info("Agent worker 启动：%s", worker_id)
        try:
            while True:
                run = tasks.claim_agent_run(worker_id)
                if run is None:
                    await asyncio.sleep(settings.agent_poll_seconds)
                    continue
                claim = ExecutionClaim(run["ticket_id"], run["id"], run["execution_epoch"], worker_id)
                stop = threading.Event()
                heartbeat = threading.Thread(target=keep_lease, args=(claim, stop), daemon=True)
                heartbeat.start()
                token = execution_claim.set(claim)
                try:
                    state = await execute_agent_run(graph, run)
                    stop.set()
                    heartbeat.join()
                    tasks.finish_agent_run(run["id"], state)
                except ExecutionLost:
                    logger.exception("执行权失效，worker 停止，禁止继续写 Checkpoint")
                    os._exit(2)
                except Exception as exc:
                    logger.exception("Agent 任务失败：run=%s", run["id"])
                    stop.set()
                    heartbeat.join()
                    tasks.fail_agent_run(run["id"], exc)
                finally:
                    execution_claim.reset(token)
                    stop.set()
                    heartbeat.join()
        finally:
            await close_checkpointer()
            reset_vector_store()


if __name__ == "__main__":
    asyncio.run(main())
