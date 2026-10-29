"""知识入库：把 knowledge/*.md 切分、向量化、写进 knowledge_chunks + 向量索引。

流程（v2.md §35）：
    管理员维护知识（Markdown / 后台接口）
      → Document
      → DocumentSplitter
      → Embedding
      → Vector Store

两个存储，各司其职（细节见 rag/index.py）：
    MySQL（knowledge_chunks）  分块正文 + 向量 —— 真相源，改稿/下线都以它为准
    向量库（Qdrant）           同一批分块的检索索引 —— 派生物，随时可从 MySQL 重灌

两个入口：
    ingest_all()        扫描 knowledge/ 目录，把新文档入库（幂等：已有分块就跳过）
    ingest_document()   单篇重新入库（后台改了内容后调用）
    reingest_all(force) 换 embedding 模型后强制重建全部向量
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import InvalidStateError
from app.core.logging import get_logger
from app.models.knowledge import KnowledgeStatus
from app.repositories import knowledge_repository
from app.rag.embeddings import get_embeddings
from app.rag.index import drop_document_index, index_chunks
from app.rag.splitter import split_markdown
from app.rag.vector_store import get_vector_store

logger = get_logger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"


# ======================================================================
# 单篇
# ======================================================================
def ingest_document(db: Session, document: dict, *, force: bool = False) -> int:
    """把一条知识文档切分 + 向量化 + 入库，返回生成的分块数。

    在调用方的事务里执行（db 由外部传入），保证"切分结果"和"文档本体"一致。

    force=False 时已经切过分块的文档直接跳过（幂等）。
    判据用 `has_chunks()` 而不是"把所有分块连向量拉出来再 any()" ——
    后者在知识量上来之后就是一次几十 MB 的无谓反序列化，只为得到一个布尔值。
    """
    document_id = document["id"]

    if not force and knowledge_repository.has_chunks(db, document_id):
        logger.info("知识文档已有分块，跳过：%s", document["title"])
        return 0

    pieces = split_markdown(document["content"])
    if not pieces:
        logger.warning("知识文档切分后为空：%s", document["title"])
        return 0

    embeddings = get_embeddings()
    vectors = embeddings.embed(pieces)

    chunks = [
        {
            "chunk_index": index,
            "content": piece,
            "embedding": vector,
            "embedding_model": embeddings.name,
            "title": document["title"],
            "source": document.get("source"),
            "category": document.get("category"),
        }
        for index, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
    ]

    current = knowledge_repository.get_document_for_update(db, document_id)
    if current is None or any(current.get(key) != document.get(key)
                              for key in ("content", "title", "category", "source", "version", "status", "updated_at")):
        raise InvalidStateError("知识文档在向量化期间发生变更，请重新发布")
    count = knowledge_repository.replace_chunks(db, document_id, chunks)
    # 分块进了 MySQL（真相源）之后，再把它们推进向量索引。
    # MySQL 后端下这一步是空操作；索引写失败会抛错、让发布整体失败（不留下"假发布"）。
    index_chunks(document_id, chunks)
    logger.info(
        "知识入库：%s → %s 块（provider=%s）",
        document["title"],
        count,
        embeddings.name,
    )
    return count


# ======================================================================
# 目录扫描 / 批量
# ======================================================================
def sync_documents_from_dir(db: Session) -> tuple[int, int]:
    """把 knowledge/ 目录下的 .md 同步成文档记录（新增/内容变更才写库）。

    返回 (新增数, 更新数)。
    """
    if not KNOWLEDGE_DIR.exists():
        logger.warning("知识目录不存在：%s", KNOWLEDGE_DIR)
        return 0, 0

    created = updated = 0
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        title = _extract_title(content, path.stem)
        source = path.name
        category = path.stem

        existing = knowledge_repository.get_document_by_source(db, source)
        if existing is None:
            knowledge_repository.create_document(
                db, title=title, content=content, source=source, category=category
            )
            created += 1
        elif existing["content"] != content:
            knowledge_repository.update_document(
                db, existing["id"], title=title, content=content
            )
            updated += 1

    if created or updated:
        logger.info("知识目录同步：新增 %s 篇，更新 %s 篇", created, updated)
    return created, updated


def ingest_all(*, force: bool = False) -> dict:
    """同步目录 + 把没有向量的文档入库。

    **本函数只负责"应用启动 / 批量重建"**，
    管理员点"发布"走的是 services/knowledge_service.publish_document()（单篇、带发布语义）。
    两者的区别很关键：这里不改变任何文档的**状态**（不把草稿悄悄发布上线），
    只补齐知识目录里已有分块的文档的向量。
    """
    with session_scope() as db:
        created, updated = sync_documents_from_dir(db)
        # 目录里同步出来的文档天然就是"已发布"（它们就是产品自带的知识）
        documents, _ = knowledge_repository.list_documents(
            db, limit=500, status=KnowledgeStatus.ENABLED
        )
        # 草稿是不该被检索的：顺手清掉它们可能残留的分块（例如某篇发布后被改回草稿）
        dropped = _drop_draft_chunks(db)

    if dropped:
        logger.info("清理草稿文档的残留分块：%s 篇", dropped)

    documents_count = 0
    chunks_count = 0
    with session_scope() as db:
        for document in documents:
            count = ingest_document(db, document, force=force)
            if count:
                documents_count += 1
                chunks_count += count

    logger.info(
        "知识入库完成：新增文档 %s，更新文档 %s，重新切分 %s 篇 / %s 块",
        created,
        updated,
        documents_count,
        chunks_count,
    )
    return {
        "documents": documents_count,
        "chunks": chunks_count,
        "created": created,
        "updated": updated,
        "embedding": get_embeddings().name,
    }


def _drop_draft_chunks(db: Session) -> int:
    """清掉草稿文档的分块 —— 草稿不该有任何向量存在（索引里的点也一起清）。

    为什么需要：文档被改过正文后会退回 DRAFT，此时分块已经清了；
    但手工改库 / 老版本留下的数据里可能有"状态是草稿、分块还在"的行，
    它们会被检索过滤掉（安全），却白白占着向量存储并被反复反序列化。
    """
    documents, _ = knowledge_repository.list_documents(
        db, limit=500, status=KnowledgeStatus.DRAFT
    )
    dropped = 0
    for document in documents:
        if knowledge_repository.has_chunks(db, document["id"]):
            knowledge_repository.clear_chunks(db, document["id"])
            drop_document_index(document["id"])
            dropped += 1
    return dropped


def reingest_document(document_id: int) -> int:
    """单篇重建（后台改完内容后调用）。"""
    with session_scope() as db:
        document = knowledge_repository.get_document(db, document_id)
        if document is None:
            from app.core.exceptions import KnowledgeNotFoundError

            raise KnowledgeNotFoundError(f"知识文档 {document_id} 不存在")
        return ingest_document(db, document, force=True)


def _extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def store_size() -> int:
    """库里现在有多少分块（启动时用它判断"要不要首次入库"）。

    数的是 **MySQL 的分块表**（真相源），而不是向量索引里的点数：
    索引可以从分块重灌（见 rag/index.py），分块在不在才是
    "这份知识入过库没有"的判据。两者会不会不一致？
    会 —— 首次切到 Qdrant 时索引是空的，启动自检会把分块搬过去（不重算向量）。
    """
    with session_scope() as db:
        return knowledge_repository.count_chunks(db)


__all__ = [
    "KNOWLEDGE_DIR",
    "ingest_all",
    "ingest_document",
    "reingest_document",
    "store_size",
    "sync_documents_from_dir",
    "get_vector_store",
]
