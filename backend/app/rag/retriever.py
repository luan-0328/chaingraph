"""检索器：把"用户问题"变成"可以喂给模型的知识片段 + 引用"。

对应 v2.md §21 / §23：
    AgentDecision(need_retrieval)
      → 生成检索 query
      → Embedding
      → Vector Store Top K
      → Context 写入 State
      → 同时保存 references（前端要展示"参考知识：退款规则 / refund_policy.md"）
"""

from app.core.logging import get_logger
from app.rag.embeddings import get_embeddings
from app.rag.vector_store import get_vector_store

logger = get_logger(__name__)

DEFAULT_TOP_K = 4
MIN_SCORE = 0.05     # 分数太低视为"没查到相关知识"，避免拿噪音去干扰模型


def retrieve(query: str, *, top_k: int = DEFAULT_TOP_K, category: str | None = None) -> list[dict]:
    """检索相关知识片段。返回按相关度倒序的命中列表。"""
    if not query or not query.strip():
        return []

    embeddings = get_embeddings()
    vector = embeddings.embed([query])[0]
    hits = get_vector_store().search(vector, top_k=top_k, category=category)
    hits = [hit for hit in hits if hit["score"] >= MIN_SCORE]

    logger.info(
        "知识检索：query=%r 命中=%s 条（provider=%s）",
        query[:40],
        len(hits),
        embeddings.name,
    )
    for hit in hits:
        logger.debug("  · %s (%s) score=%s", hit["title"], hit["source"], hit["score"])

    return hits


def build_context(hits: list[dict], *, max_chars: int = 2400) -> str:
    """把命中片段拼成给 LLM 的上下文（带来源标记，便于模型引用）。"""
    if not hits:
        return ""

    parts: list[str] = []
    used = 0
    for index, hit in enumerate(hits, start=1):
        block = f"[{index}] 来源：{hit['title']}（{hit['source']}）\n{hit['content']}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def to_references(hits: list[dict]) -> list[dict]:
    """转成 AgentResult.references（只留引用元数据，不带正文）。"""
    return [
        {
            "title": hit.get("title"),
            "source": hit.get("source"),
            "category": hit.get("category"),
            "score": hit.get("score"),
        }
        for hit in hits
    ]
