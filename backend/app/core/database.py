"""数据库层：引擎 / 会话 / 事务 / 初始化。

--------------------------------------------------------------------------
事务边界放在哪里？（这是"项目"和"demo"的关键差别之一）
--------------------------------------------------------------------------
错误做法：每个 Repository 方法自己开事务。
    这样"下单 = 建订单 + 建明细 + 扣库存 + 清购物车"会变成 4 个独立事务，
    中途失败就留下脏数据。

本项目的做法：
    **Service 拥有事务边界**，Repository 只接收 db 参数、不自己提交。

        with session_scope() as db:          # ← 一个事务
            order = order_repo.create(db, ...)
            order_item_repo.bulk_create(db, ...)
            product_repo.decrease_stock(db, ...)
            cart_repo.remove_items(db, ...)
        # 出 with 才 commit；抛异常自动 rollback

v2.md §61 要求下单、支付、退款具备事务一致性，就是这个模式。
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.db_echo,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：Service 层用它把多个 Repository 操作包成一个原子操作。

        with session_scope() as db:
            ...
        # 正常结束 commit；异常 rollback
    """
    from app.core.execution import assert_execution, execution_claim, execution_session
    claim = execution_claim.get()
    existing = execution_session.get()
    if existing is not None:
        # Worker 内部服务嵌套调用共用同一事务，避免再次锁自己的工单。
        yield existing
        return
    db = SessionLocal()
    session_token = None
    try:
        if claim is not None:
            assert_execution(db, claim)
            session_token = execution_session.set(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if session_token is not None:
            execution_session.reset(session_token)
        db.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖形式（只读接口或需要跨依赖共享 Session 时使用）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ======================================================================
# 初始化：建库 -> 建表 -> 补列（老库升级）-> 演示数据
# ======================================================================
def init_database() -> bool:
    """准备数据库。返回是否成功（失败只记日志，由 main 决定是否阻塞启动）。"""
    try:
        _create_database_if_not_exists()

        # 导入所有模型，让它们注册到 Base.metadata
        from app import models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        logger.info("数据表已就绪（%d 张）：%s", len(Base.metadata.tables), ", ".join(sorted(Base.metadata.tables)))

        # create_all 只建"缺失的表"，不会给已存在的表补列 —— 新增字段靠这一步对齐
        from app.core.schema import sync_schema

        sync_schema(engine)

        if settings.app_env == "dev":
            from app.repositories.seed import seed_all

            with session_scope() as db:
                seed_all(db)

        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("数据库初始化失败：%s", exc)
        logger.error("请检查 .env 里的 DATABASE_URL，以及 MySQL 服务是否启动")
        return False


def _create_database_if_not_exists() -> None:
    """连到 MySQL 服务器（不带库名）执行 CREATE DATABASE IF NOT EXISTS。"""
    server_engine = create_engine(settings.server_url, isolation_level="AUTOCOMMIT")
    try:
        with server_engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.database_name}` "
                    f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    finally:
        server_engine.dispose()


def check_database_connection() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("MySQL 连接失败：%s", exc)
        return False
