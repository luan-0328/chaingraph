"""独立单机 Agent worker：python -m app.worker。

进程锁覆盖整个 Checkpoint 生命周期，旧进程退出后新进程才能恢复同一文件。
本版本不支持跨机器 worker；不能把 SQLite 文件复制到另一台机器后同时运行。
"""
import asyncio
from contextlib import contextmanager, ExitStack
import hashlib
import os
import socket
import threading
import time
import uuid

from langgraph.types import Command

from app.core.config import settings, BASE_DIR
from app.core.database import init_database
from app.core.execution import ExecutionClaim, ExecutionLost, HumanHandoffRequested, execution_claim
from app.core.logging import get_logger, setup_logging
from app.services import agent_task_service as tasks

logger = get_logger(__name__)


@contextmanager
def file_lock(path):
    """锁覆盖整个文件使用期，不能运行中删除锁文件后换一个 inode 绕开互斥。"""
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
            raise RuntimeError("业务库或 Checkpoint 已有 worker；请先停止旧 worker") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def single_worker_lock():
    """分别锁业务库与实际 Checkpoint 文件，覆盖同库异文件和异库同文件的误配置。"""
    from sqlalchemy.engine import make_url
    url = make_url(settings.database_url)
    identity = f"{url.host}:{url.port or 3306}/{url.database}"
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    checkpoint = settings.checkpoint_path.resolve()
    with ExitStack() as stack:
        stack.enter_context(file_lock(BASE_DIR / "data" / f"agent-worker-{key}.lock"))
        stack.enter_context(file_lock(checkpoint.with_name(checkpoint.name + ".worker.lock")))
        yield


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
    from app.agent.version import compatible
    from app.agent.graph import check_handoff_boundary

    await asyncio.to_thread(check_handoff_boundary)

    if not compatible(run):
        raise RuntimeError("AGENT_VERSION_MISMATCH：流程、Prompt 或模型版本不兼容，禁止恢复；请人工核实")

    config = {"configurable": {"thread_id": run["thread_id"]},
              "recursion_limit": RECURSION_LIMIT, "callbacks": [NodeTrackingCallback(run["id"])]}
    snapshot = await graph.aget_state(config)
    saved = snapshot.values or {}
    if saved and (saved.get("agent_run_id") != run["id"] or saved.get("flow_version") != run["flow_version"]):
        raise RuntimeError("CHECKPOINT_VERSION_MISMATCH：Checkpoint 不属于当前任务或流程版本")
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
    ticket = await asyncio.to_thread(get_ticket_raw, run["ticket_id"])
    initial = _build_initial_state(ticket=ticket, agent_run_id=run["id"],
        thread_id=run["thread_id"], content=ticket["content"])
    return await graph.ainvoke(initial, config=config)


async def run_one(graph, run, worker_id):
    """一个任务独占自己的执行凭证与心跳；不同任务可在同一事件循环中并发。"""
    claim = ExecutionClaim(run["ticket_id"], run["id"], run["execution_epoch"], worker_id)
    stop = threading.Event()
    heartbeat = threading.Thread(target=keep_lease, args=(claim, stop), daemon=True)
    heartbeat.start()
    token = execution_claim.set(claim)
    try:
        state = await execute_agent_run(graph, run)
        stop.set()
        await asyncio.to_thread(heartbeat.join)
        await asyncio.to_thread(tasks.finish_agent_run, run["id"], state)
    except ExecutionLost:
        logger.exception("执行权失效，worker 停止，禁止继续写 Checkpoint")
        os._exit(2)
    except HumanHandoffRequested:
        from app.services.handoff_service import complete_worker_handoff
        stop.set()
        await asyncio.to_thread(heartbeat.join)
        await asyncio.to_thread(complete_worker_handoff, claim)
    except Exception as exc:
        logger.exception("Agent 任务失败：run=%s", run["id"])
        stop.set()
        await asyncio.to_thread(heartbeat.join)
        try:
            await asyncio.to_thread(tasks.fail_agent_run, run["id"], exc)
        except HumanHandoffRequested:
            from app.services.handoff_service import complete_worker_handoff
            await asyncio.to_thread(complete_worker_handoff, claim)
    finally:
        execution_claim.reset(token)
        stop.set()
        await asyncio.to_thread(heartbeat.join)


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
        logger.info("Agent worker 启动：%s 并发上限=%s", worker_id, settings.agent_max_concurrency)
        running = set()
        next_review_sweep = 0.0
        next_upload_cleanup = 0.0
        cleanup_task = None

        async def clean_images():
            # OSS 慢请求不能阻塞调度；清理失败保留数据库意图供下轮重试。
            try:
                from app.services.attachment_service import cleanup_uploads
                await asyncio.to_thread(cleanup_uploads)
            except Exception:
                logger.exception("图片定期清理失败，将稍后重试")

        try:
            while True:
                for task in tuple(running):
                    if task.done():
                        running.remove(task)
                        task.result()
                if time.monotonic() >= next_review_sweep:
                    await asyncio.to_thread(tasks.sweep_reviews)
                    next_review_sweep = time.monotonic() + 1.0
                if cleanup_task is not None and cleanup_task.done():
                    cleanup_task.result()
                    cleanup_task = None
                if cleanup_task is None and time.monotonic() >= next_upload_cleanup:
                    cleanup_task = asyncio.create_task(clean_images())
                    next_upload_cleanup = time.monotonic() + 60.0
                if len(running) >= settings.agent_max_concurrency:
                    await asyncio.wait(running, timeout=settings.agent_poll_seconds,
                                       return_when=asyncio.FIRST_COMPLETED)
                    continue
                run = await asyncio.to_thread(tasks.claim_agent_run, worker_id)
                if run is not None:
                    running.add(asyncio.create_task(run_one(graph, run, worker_id)))
                else:
                    await asyncio.sleep(settings.agent_poll_seconds)
        finally:
            if any(not task.done() for task in running) or (cleanup_task is not None and not cleanup_task.done()):
                # 协程取消不能终止正在运行的工具线程，异常关机必须退出整个进程。
                os._exit(2)
            await close_checkpointer()
            reset_vector_store()


if __name__ == "__main__":
    asyncio.run(main())
