"""全局配置：所有可变项都从 .env 读，代码里不写死任何密钥。

新增配置只要在这里加一个字段，.env 里同名（大小写不敏感）就会自动生效。
"""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> parents[2] == backend/
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置。"""

    # ---------------- 应用 ----------------
    app_env: str = "dev"
    app_name: str = "智能电商客服与售后工单平台"
    app_version: str = "2.0.0"

    # ---------------- 数据库 ----------------
    # 直接给完整连接串（v2.md §59），换数据库只改 .env
    database_url: str = "mysql+pymysql://root:your_password@localhost:3306/shop_ticket?charset=utf8mb4"
    db_echo: bool = False

    # ---------------- 鉴权 ----------------
    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    # ---------------- LLM ----------------
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_temperature: float = 0.0
    llm_timeout: float = 90.0

    # DeepSeek 默认开启思考模式（thinking），但思考模式下**不支持强制 tool_choice**，
    # 而结构化输出（with_structured_output method=function_calling）正是靠强制 tool_choice 实现的。
    # 所以默认关掉思考模式：结构化抽取类任务不需要长思考，同时更快更省。
    # 想开启就设 LLM_DISABLE_THINKING=false（届时结构化输出需要改用 json_mode）。
    llm_disable_thinking: bool = True

    # ---------------- Embedding（RAG） ----------------
    # 留空 -> 使用本地降级向量器，链路可跑但语义效果弱
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_dim: int = 0
    # 检索参数必须按实际 Embedding 和评测集校准，保留原默认值便于回归对照。
    rag_top_k: int = 4
    rag_min_score: float = 0.05

    # ---------------- 向量库（RAG 检索后端） ----------------
    # qdrant = 索引与检索走 Qdrant（默认）；mysql = 旧实现（全量 numpy 余弦，作为回退保留）
    vector_store: str = "qdrant"
    # Qdrant 地址：留空 -> 本地内嵌模式（数据落在 QDRANT_PATH，不用起服务、独占目录）
    # 填 http://127.0.0.1:6333 -> 服务端模式（多实例/多进程并发时就该用它）
    qdrant_url: str = ""
    # Qdrant Cloud 或开了鉴权的自建服务才需要
    qdrant_api_key: str = ""
    # 本地内嵌模式的存储目录（同一目录同时只能被一个进程打开，锁冲突时自动回退 MySQL）
    qdrant_path: str = "./data/qdrant"
    qdrant_collection: str = "knowledge_chunks"
    qdrant_timeout: float = 15.0

    # ---------------- 业务规则 ----------------
    refund_auto_approve_limit: float = 500.0
    logistics_stalled_hours: int = 72
    agent_max_retries: int = 2
    # 可恢复异常重试时，是否优先"从上一轮的 Checkpoint 断点续跑"。
    # 开（默认）：图已经跑过的只读节点（分析/检索/查工具）不重跑，只补做失败的那一步；
    # 关：每次重试用完整初始 State 从头跑，清除旧审批和输出，仍复用当前 run/thread。
    # 注意：只要副作用节点（execute_action / human_review）跑过，无论开关如何都会从头跑。
    agent_resume_from_checkpoint: bool = True

    # ---------------- Agent / Checkpoint ----------------
    checkpoint_db_path: str = "./data/checkpoints.sqlite"
    agent_lease_seconds: int = 120
    agent_heartbeat_seconds: int = 15
    agent_poll_seconds: float = 1.0
    agent_max_concurrency: int = 4
    # 审批到期不会自动批准或退款；交回客服继续处理。
    agent_review_timeout_seconds: int = 86400
    agent_review_reminder_seconds: int = 3600

    # 确定性 LLM 桩：只在并发验收测试时打开（AGENT_STUB_LLM=1），
    # 让"每次执行的耗时"可控，从而稳定地测出"同工单互斥、异工单并发"。
    # 正常启动保持关闭，走真实模型。
    agent_stub_llm: bool = False

    # OSS 使用私有桶，凭据只在后端读取；留空时禁用图片上传，不影响文字对话。
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_security_token: str = ""
    oss_region: str = ""
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_max_upload_bytes: int = 5 * 1024 * 1024
    oss_signed_url_seconds: int = 300

    # ---------------- CORS ----------------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _blank_means_default(cls, data):
        """`.env` 里写 `FOO=`（留空）时按"没配置"处理，用代码默认值。

        真实项目里 .env 经常留一堆空值（例如 embedding key 还没申请），
        如果没有这条规则，空的 int 字段会直接让应用起不来。
        """
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value not in ("", None)}
        return data

    @model_validator(mode="after")
    def validate_deployment_security(self):
        if not 1 <= self.rag_top_k <= 50 or not 0 <= self.rag_min_score <= 1:
            raise ValueError("RAG_TOP_K 必须为 1～50，RAG_MIN_SCORE 必须在 0～1 之间")
        if not 1 <= self.oss_max_upload_bytes <= 10 * 1024 * 1024:
            raise ValueError("OSS_MAX_UPLOAD_BYTES 必须在 1 到 10 MB 之间")
        if not 60 <= self.oss_signed_url_seconds <= 3600:
            raise ValueError("OSS_SIGNED_URL_SECONDS 必须在 60 到 3600 秒之间")
        if not 1 <= self.agent_max_concurrency <= 32:
            raise ValueError("AGENT_MAX_CONCURRENCY 必须在 1 到 32 之间")
        if not 0 < self.agent_review_reminder_seconds < self.agent_review_timeout_seconds:
            raise ValueError("审批提醒间隔必须大于 0 且小于审批期限")
        if self.app_env.lower() not in ("dev", "development", "test", "testing"):
            if len(self.jwt_secret) < 32 or self.jwt_secret == "dev-only-change-me":
                raise ValueError("非开发环境必须配置至少 32 字符的 JWT_SECRET")
            if self.agent_stub_llm:
                raise ValueError("非开发环境禁止启用 AGENT_STUB_LLM 和测试接口")
        return self

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def server_url(self) -> str:
        """去掉库名的连接串（用于 CREATE DATABASE）。"""
        base, _, _ = self.database_url.partition("?")
        return base.rsplit("/", 1)[0] + "/?charset=utf8mb4"

    @property
    def database_name(self) -> str:
        base, _, _ = self.database_url.partition("?")
        return base.rsplit("/", 1)[-1]

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def embedding_configured(self) -> bool:
        """是否配置了真实 embedding 服务（否则走本地降级向量器）。"""
        return bool(self.embedding_api_key.strip() and self.embedding_model.strip())

    @property
    def qdrant_mode(self) -> str:
        """Qdrant 的工作模式：`server`（连服务）/ `local`（进程内嵌，数据落磁盘）。

        为什么要分两种：本地内嵌模式**零部署**（不用 Docker、不用起服务），
        适合单进程开发与测试；但它的存储目录带**独占文件锁** ——
        多实例（pytest 会同时拉起 3 个 uvicorn）会打不开，那时必须用 `server`。
        """
        return "server" if self.qdrant_url.strip() else "local"

    @property
    def qdrant_location(self) -> str:
        """Qdrant 的位置描述（日志/健康检查里一眼看出连的是哪儿）。"""
        if self.qdrant_mode == "server":
            return self.qdrant_url.strip()
        return str(self.qdrant_local_path)

    @property
    def qdrant_local_path(self) -> Path:
        """本地内嵌模式的存储目录（相对路径按 backend/ 解析，和 Checkpoint 一致）。"""
        path = Path(self.qdrant_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def checkpoint_path(self) -> Path:
        path = Path(self.checkpoint_db_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
