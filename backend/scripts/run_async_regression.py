"""在一次性数据库中验证当前异步 API 与独立 worker，退出时回收所有运行产物。"""

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from app.core.config import settings  # noqa: E402


def stop_process_group(process):
    """即使 pytest 被中断，也终止它启动的 API 和 worker 子进程。"""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
        process.wait(timeout=10)


def main():
    """只修改本次生成的数据库；测试脚本及源码保留，日志和 Checkpoint 自动删除。"""
    name = "agent_async_test_" + uuid.uuid4().hex
    arguments = list(sys.argv[1:])
    browser = "--browser" in arguments
    if browser:
        arguments.remove("--browser")
    original = make_url(settings.database_url)
    # URL.set(database=None) 会保留原库名；_replace 才真正移除库名。
    server = create_engine(original._replace(database=None), isolation_level="AUTOCOMMIT")
    identity = f"{original.host}:{original.port or 3306}/{name}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    lock = ROOT / "data" / f"agent-worker-{digest}.lock"
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="chaingraph_async_") as folder:
            env = os.environ.copy()
            # 显式加载测试插件，隔离真实模型配置，回归不访问模型服务。
            env.pop("PYTEST_ADDOPTS", None)
            env.update(
                DATABASE_URL=original.set(database=name).render_as_string(hide_password=False),
                OSS_ACCESS_KEY_ID="", OSS_ACCESS_KEY_SECRET="", OSS_REGION="", OSS_BUCKET="",
                APP_ENV="dev", LLM_API_KEY="", EMBEDDING_API_KEY="", EMBEDDING_MODEL="",
                VECTOR_STORE="mysql", AGENT_STUB_LLM="1", AGENT_STUB_DELAY_SECONDS="0.05",
                AGENT_STUB_BUSINESS_ACTION="apply_refund", AGENT_STUB_HUMAN_TICKETS="",
                AGENT_STUB_FAIL_TICKETS="", AGENT_STUB_FATAL_TICKETS="",
                AGENT_LEASE_SECONDS="6", AGENT_HEARTBEAT_SECONDS="1", AGENT_POLL_SECONDS="0.05",
                AGENT_MAX_RETRIES="2", CHECKPOINT_DB_PATH=str(Path(folder) / "checkpoints.sqlite"),
                AGENT_MAX_CONCURRENCY="4", AGENT_REVIEW_TIMEOUT_SECONDS="86400",
                AGENT_REVIEW_REMINDER_SECONDS="3600",
                QDRANT_PATH=str(Path(folder) / "qdrant"), ASYNC_TEST_DATABASE=name,
                ASYNC_TEST_FOLDER=folder, PYTHONDONTWRITEBYTECODE="1",
                PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
            )
            print("初始化独立测试数据库（不读写原业务库）", flush=True)
            with open(Path(folder) / "init.log", "w+") as log:
                result = subprocess.run(
                    [sys.executable, "-c", "from app.core.database import init_database; assert init_database()"],
                    cwd=ROOT, env=env, stdout=log, stderr=log,
                )
                if result.returncode:
                    log.seek(0)
                    print(log.read())
                    return result.returncode
            env["APP_ENV"] = "test"
            # 定位新测试目录，不混入依赖旧同步契约的历史用例。
            # 显式列出默认用例，避免目录与额外浏览器文件同时传入时被 pytest 去重漏收集。
            paths = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests_async").glob("test_*.py"))]
            if browser:
                paths.append("tests_async/browser_review.py")
            command = [sys.executable, "-m", "pytest", *paths, "-q", "--tb=short",
                       "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin",
                       "--basetemp", str(Path(folder) / "pytest"),
                       *arguments]
            process = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=os.name == "posix")
            try:
                return process.wait()
            finally:
                stop_process_group(process)
    finally:
        if process is not None and process.poll() is None:
            stop_process_group(process)
        try:
            with server.connect() as db:
                db.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        finally:
            server.dispose()
            lock.unlink(missing_ok=True)
            try:
                lock.parent.rmdir()
            except OSError:
                pass
        print("清理完成：测试数据库、子进程、日志、Checkpoint、worker 锁", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
