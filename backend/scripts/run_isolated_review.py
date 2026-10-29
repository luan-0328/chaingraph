"""在一次性 MySQL 数据库与临时 Checkpoint 中执行回归，不写演示业务数据。"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import settings


def main():
    for port in (8021, 8022, 8023):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"测试端口 {port} 已被占用，拒绝复用未知实例")
    name = "agent_review_" + uuid.uuid4().hex
    original = make_url(settings.database_url)
    server = create_engine(original.set(database=None), isolation_level="AUTOCOMMIT")
    with tempfile.TemporaryDirectory(prefix="agent_review_") as folder:
        env = os.environ.copy()
        env.update(DATABASE_URL=original.set(database=name).render_as_string(hide_password=False),
                   APP_ENV="dev", EMBEDDING_API_KEY="", EMBEDDING_MODEL="", LLM_API_KEY="",
                   AGENT_STUB_LLM="0", VECTOR_STORE="mysql",
                   CHECKPOINT_DB_PATH=str(Path(folder) / "checkpoints.sqlite"),
                   QDRANT_PATH=str(Path(folder) / "qdrant"))
        try:
            subprocess.run([sys.executable, "-c", "from app.core.database import init_database; assert init_database()"],
                           cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
            return subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", *sys.argv[1:]],
                                  cwd=ROOT, env=env).returncode
        finally:
            with server.connect() as db:
                db.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
            server.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
