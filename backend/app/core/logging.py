"""统一日志：格式、降噪、request_id 关联。

request_id 的实现方式：
    中间件在每个请求开始时生成/透传一个 id，存进 ContextVar；
    日志过滤器把它塞进每条日志的 %(request_id)s 字段。
    这样一次请求涉及的多层日志（router/service/repository/agent）能串起来 —— 排查线上问题时非常关键。
"""

import logging
import uuid
from contextvars import ContextVar

from app.core.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(request_id)s | %(name)-30s | %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

# 每个请求一个 id（协程安全：ContextVar 在不同请求/任务之间互不干扰）
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# 这些库日志太吵，统一压到 WARNING
NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "httpx2",
    "httpcore2",
    "urllib3",
    "openai",
    "langchain_openai",
    "sqlalchemy.engine",   # 想看 SQL 就把这行删掉，或设 DB_ECHO=true
    "aiosqlite",           # Checkpoint 用的 SQLite 驱动，DEBUG 下会刷屏
    "asyncio",
)


class RequestIdFilter(logging.Filter):
    """把 ContextVar 里的 request_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging() -> None:
    level = logging.DEBUG if settings.app_env == "dev" else logging.INFO

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
