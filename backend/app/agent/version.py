"""固定任务执行契约，避免部署后用新流程或新模型盲目恢复旧 Checkpoint。"""

from functools import lru_cache
import hashlib
from importlib.metadata import version
import json

from app.core.config import BASE_DIR, settings


@lru_cache(maxsize=1)
def execution_spec() -> dict:
    """生成不含密钥的代码、Prompt、依赖和模型指纹；单个进程启动后保持不变。"""
    digest = hashlib.sha256()
    # 保守校验整个后端执行代码，避免只更新 Service 却错误宣称流程版本兼容。
    for path in sorted((BASE_DIR / "app").rglob("*.py")):
        digest.update(str(path.relative_to(BASE_DIR)).encode())
        digest.update(path.read_bytes())
    prompt_hash = hashlib.sha256((BASE_DIR / "app/agent/prompts.py").read_bytes()).hexdigest()
    spec = {
        "code": digest.hexdigest(), "prompt": prompt_hash,
        "dependencies": {name: version(name) for name in (
            "langgraph", "langgraph-checkpoint-sqlite", "langchain-core", "langchain-openai")},
        "model": {"name": settings.llm_model, "base_url": settings.llm_base_url,
                  "temperature": settings.llm_temperature, "disable_thinking": settings.llm_disable_thinking,
                  "stub": settings.agent_stub_llm},
        "embedding": {"name": settings.embedding_model if settings.embedding_configured else "local-hash-ngram",
                      "base_url": settings.embedding_base_url if settings.embedding_configured else "",
                      "dimension": settings.embedding_dim if settings.embedding_configured else 512},
        "business": {"refund_limit": settings.refund_auto_approve_limit,
                     "logistics_stalled_hours": settings.logistics_stalled_hours},
        "retrieval": {"top_k": settings.rag_top_k, "min_score": settings.rag_min_score},
    }
    spec["version"] = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    return spec


def compatible(run: dict) -> bool:
    """缺少版本的旧任务也不可猜测兼容性，需人工核实或使用原版本处理。"""
    return bool(run.get("flow_version") and run["flow_version"] == execution_spec()["version"])
