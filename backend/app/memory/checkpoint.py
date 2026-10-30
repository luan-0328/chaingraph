"""Checkpoint：图状态持久化 —— HITL 能在几分钟/几小时后继续执行的前提。

v2.md §32 说得直白：
    "HITL 最大的问题是：Agent 暂停之后，状态放在哪里？"
答案就是 Checkpoint：把图的完整 State 按 thread_id 存下来，
人工审批后带着同一个 thread_id 恢复，图从断点继续跑。

实现选择：
    langgraph-checkpoint-sqlite 的 AsyncSqliteSaver —— 落一个本地 SQLite 文件。
    为什么不用内存版？内存版进程重启就丢，"客服明天再来审批"就恢复不了。
    为什么不用 Postgres？V1 明确不引入额外基础设施（v2.md §4.4）。

生命周期：
    仅 worker 启动时打开、停止时关闭。同一 worker 内多个任务共用一个 Saver，
    Saver 自身的 asyncio.Lock 串行化数据库操作，不串行化完整工单执行。
"""

from contextlib import AsyncExitStack

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_stack: AsyncExitStack | None = None
_saver: AsyncSqliteSaver | None = None


async def init_checkpointer() -> AsyncSqliteSaver:
    """启动时调用一次：打开 SQLite checkpoint 存储。"""
    global _stack, _saver

    if _saver is not None:
        return _saver

    stack = AsyncExitStack()
    try:
        saver = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path))
        )
        await saver.setup()
        # WAL 由 Saver.setup 开启；忙等待与 FULL 同步保护本地并发及提交持久性。
        await saver.conn.execute("PRAGMA busy_timeout=5000")
        await saver.conn.execute("PRAGMA synchronous=FULL")
    except BaseException:
        await stack.aclose()
        raise
    _stack, _saver = stack, saver
    logger.info("Checkpoint 存储已就绪：%s", settings.checkpoint_path)
    return _saver


async def close_checkpointer() -> None:
    """关闭时调用：释放 SQLite 连接。"""
    global _stack, _saver

    if _stack is not None:
        await _stack.aclose()
        logger.info("Checkpoint 存储已关闭")
    _stack = None
    _saver = None


def get_checkpointer() -> AsyncSqliteSaver:
    """取当前 saver（必须在 init_checkpointer 之后调用）。"""
    if _saver is None:
        raise RuntimeError("Checkpointer 未初始化：请确认应用启动时调用了 init_checkpointer()")
    return _saver


def is_ready() -> bool:
    return _saver is not None
