"""知识库服务 —— 知识文档的增删改查 + **发布 / 下线**（v2.md §34 / §35）。

为什么知识库要有 Service 层
--------------------------
管理端接口原来直接调 repository + ingest：看起来省事，但"发布"这件事
**不是一次 CRUD** —— 它是"改状态 + 切分 + 向量化 + 写向量库"的组合动作，
而且顺序不能反：

    先算向量、成功之后再置 ENABLED
    （反过来的话，向量化失败会留下"状态是已发布、但检索不到"的假发布）

事务边界也和普通 CRUD 不同：
    `session_scope()` 一次只包住"读文档 / 写状态"这种短事务；
    切分 + 向量化（可能是一次外部 Embedding API 调用，几百毫秒到几秒）
    **绝不放在事务里** —— 那样会把一个 HTTP 请求的等待时间变成数据库锁的持有时间。

对外提供
--------
    list_documents / get_document                       查询
    create_document                                     新增（DRAFT，等待发布）
    update_document                                     修改（改了正文 → 自动退回 DRAFT）
    delete_document                                     删除（连带分块与向量索引里的点）
    publish_document                                    发布：切分 → Embedding → 写向量库
    offline_document                                    下线：不再参与检索（分块保留）
    reindex                                             把库里的向量整体重灌进向量索引
    search                                              检索调试（看得到分数与出处）

两个存储（发布/删除都要照顾到，别只清一边）
    分块与向量   knowledge_chunks（MySQL）—— 真相源，检索的最终判据
    向量索引     Qdrant —— 派生物，只在"发布"与"清理"两处跟着动（见 app/rag/index.py）
"""

from app.core.database import session_scope
from app.core.exceptions import KnowledgeNotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.knowledge import KnowledgeStatus
from app.repositories import knowledge_repository
from app.rag.index import drop_document_index, index_status, rebuild_index
from app.rag.ingest import ingest_document
from app.rag.retriever import retrieve

logger = get_logger(__name__)


# ======================================================================
# 查询
# ======================================================================
def list_documents(
    *,
    limit: int = 20,
    offset: int = 0,
    category: str | None = None,
    keyword: str | None = None,
    status: str | None = None,
) -> dict:
    """文档列表。带每篇的**分块数**，前端一眼能看出"发布过没有"。"""
    with session_scope() as db:
        items, total = knowledge_repository.list_documents(
            db, limit=limit, offset=offset, category=category, keyword=keyword, status=status
        )
        chunk_counts = knowledge_repository.count_chunks_by_document(
            db, [item["id"] for item in items]
        )

    for item in items:
        item["chunk_count"] = chunk_counts.get(item["id"], 0)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_document(document_id: int) -> dict:
    """单篇详情（带分块数，前端据此判断"发布过没有"）。"""
    document = _get_or_404(document_id)
    with session_scope() as db:
        counts = knowledge_repository.count_chunks_by_document(db, [document_id])
    document["chunk_count"] = counts.get(document_id, 0)
    return document


def search(query: str, *, top_k: int = 4) -> list[dict]:
    """检索调试：管理员能直接看到命中分数与出处（不经过 Agent）。"""
    return retrieve(query, top_k=top_k)


def reindex() -> dict:
    """把 MySQL 里的分块与向量整体重灌进向量索引（**不重新调用 Embedding**）。

    什么时候用：
        · 刚把向量库换成 Qdrant（第一次启动其实会自动做，见 rag/index.py 的启动自检）
        · 索引被清空 / 换机器 / 有人手工动过库
        · 知识库概览里 `in_sync=false`（点数与分块数对不上）
    它只搬向量，不改正文、不改状态 —— 所以不会把草稿"发布"出去。
    """
    result = rebuild_index(recreate=True)
    logger.info("手工重建向量索引：%s", result)
    return {**result, "status": index_status()}


def stats() -> dict:
    """知识库概览：多少篇草稿 / 已发布 / 已下线，一共多少块向量，索引在不在同步状态。"""
    with session_scope() as db:
        counts = knowledge_repository.count_documents_by_status(db)
        chunks = knowledge_repository.count_chunks(db)
        indexed = knowledge_repository.count_indexed_chunks(db)
    return {
        "documents": counts,
        "chunks": {"total": chunks, "indexed": indexed},
        "retrievable": counts.get(KnowledgeStatus.ENABLED, 0),
        # 向量库现状（后端 / 位置 / 点数 / 是否与库一致）——换过向量库后最该看的就是这一项
        "vector_store": index_status(),
    }


# ======================================================================
# 写：增 / 改 / 删
# ======================================================================
def create_document(
    *,
    title: str,
    content: str,
    category: str | None = None,
    source: str | None = None,
    publish: bool = False,
) -> dict:
    """新增知识文档。

    默认落在 **DRAFT**：新知识要经管理员显式"发布"才进向量库。
    理由见 models/knowledge.py 的文档生命周期 —— 让"哪份知识在生效"
    变成一个明确的动作，而不是"不小心建了就上线了"。
    """
    with session_scope() as db:
        document = knowledge_repository.create_document(
            db,
            title=title,
            content=content,
            category=category,
            source=source,
            status=KnowledgeStatus.DRAFT,
        )

    if publish:
        # publish 只在请求里显式要求时才做（切分 + 向量化在事务外执行）
        return _publish(document["id"])

    logger.info("新增知识文档（草稿）：id=%s title=%s", document["id"], title)
    document["chunk_count"] = 0
    return document


def update_document(
    *,
    document_id: int,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
    status: str | None = None,
) -> dict:
    """修改知识文档。

    关键规则：**改了正文就自动退回 DRAFT**。
        正文变了而向量还是旧的 → 检索命中的是过时知识，比检索不到更危险。
        退回草稿逼管理员重新发布一次，发布动作里才真正重算分块与向量。

    只改标题 / 分类（不影响语义）时保持原状态，不用重新发布。
    """
    _validate_status(status)

    current = _get_or_404(document_id)
    content_changed = content is not None and content != current["content"]

    fields: dict = {}
    if title is not None:
        fields["title"] = title
    if content is not None:
        fields["content"] = content
    if category is not None:
        fields["category"] = category

    if content_changed:
        fields["status"] = KnowledgeStatus.DRAFT
        # 正文变了 → 旧向量必须作废，否则会拿旧知识回答（宁可先检索不到）
        fields["version"] = _bump_version(current.get("version") or "1.0")
    elif status is not None:
        fields["status"] = status

    with session_scope() as db:
        document = knowledge_repository.update_document(db, document_id, **fields)
        if document is None:
            raise KnowledgeNotFoundError(f"知识文档 {document_id} 不存在")

    if content_changed:
        # 内容变了，旧分块与本事已对不上 → 直接丢掉（下次发布时重建）
        _purge_chunks(document_id)
        logger.info(
            "知识文档正文变更 → 退回草稿并作废旧向量：id=%s version=%s",
            document_id,
            fields.get("version"),
        )

    return get_document(document_id)


def delete_document(document_id: int) -> None:
    """删除文档，**连带它的分块与向量**（否则向量库里会留下孤儿，永远被检索到）。"""
    with session_scope() as db:
        knowledge_repository.clear_chunks(db, document_id)
        if not knowledge_repository.delete_document(db, document_id):
            raise KnowledgeNotFoundError(f"知识文档 {document_id} 不存在")
    # 分块已随事务清掉，向量索引里的点再单独清一次（两个存储都要清）
    drop_document_index(document_id)
    logger.info("删除知识文档（含分块与向量索引）：id=%s", document_id)


# ======================================================================
# 发布 / 下线（本次新增的核心）
# ======================================================================
def publish_document(document_id: int) -> dict:
    """发布一篇知识：**切分 → Embedding → 写入向量库**，然后才置成 ENABLED。

    顺序不能反（这是本函数存在的全部理由）：
        1. 先把正文切成分块、算好向量、写进 knowledge_chunks；
        2. 全部成功之后再 `status = ENABLED`。
    反过来的话，Embedding 挂了就会留下"状态显示已发布、实际检索不到"的假发布 ——
    管理员以为上线了，Agent 却永远查不到。

    分块与向量写在**独立事务**里，且不与外部 Embedding 调用同事务：
    向量化可能要几秒，不该占着数据库连接与行锁。
    """
    return _publish(document_id)


def offline_document(document_id: int) -> dict:
    """下线一篇知识：不再参与检索，但**保留分块与向量**。

    为什么保留分块：下线通常是"临时停用/正在修订"，重新发布时可以直接复用
    已有向量（知识量大的时候省掉一整轮 Embedding 费用）。
    检索侧的过滤条件是 `knowledge_documents.status = ENABLED`，
    所以下线后 Agent 一定检索不到它 —— 这是唯一的开关，没有第二个地方要改。
    """
    document = _get_or_404(document_id)
    if document["status"] == KnowledgeStatus.DISABLED:
        raise ValidationError("该知识文档已经是下线状态")

    with session_scope() as db:
        updated = knowledge_repository.update_document(
            db, document_id, status=KnowledgeStatus.DISABLED
        )
    logger.info("知识文档下线（不再参与检索）：id=%s title=%s", document_id, document["title"])
    return _with_chunk_count(updated, document_id)


def publish_all_drafts() -> dict:
    """一键发布所有草稿（批量导入知识后用）。返回发布了几篇 / 几个分块。"""
    with session_scope() as db:
        documents, _ = knowledge_repository.list_documents(
            db, limit=500, status=KnowledgeStatus.DRAFT
        )

    published: list[int] = []
    failed: list[dict] = []
    chunks = 0
    for document in documents:
        try:
            result = _publish(document["id"], rebuild=True)
            published.append(document["id"])
            chunks += result.get("chunk_count", 0)
        except Exception as exc:  # noqa: BLE001 —— 一篇失败不该拖垮整批
            logger.warning("发布知识文档失败：id=%s error=%s", document["id"], exc)
            failed.append({"document_id": document["id"], "error": str(exc)})

    return {"published": len(published), "chunks": chunks, "failed": failed}


# ======================================================================
# 内部
# ======================================================================
def _publish(document_id: int, *, rebuild: bool = True) -> dict:
    """真正干活的发布实现。

    和 publish_document 的差别只在于**是不是无条件重建向量**：
        publish_document（管理员点"发布"）→ rebuild=True
            "我发布了"必须保证向量是当前正文算出来的，不能用旧缓存。
        应用启动时同步目录               → rebuild=False
            已经有分块的文档直接跳过，避免每次重启都重算 57 个向量。

    失败处理：向量化抛异常 → 状态**不改成 ENABLED**（停在 DRAFT），
    并把错误原样抛给调用方 —— 宁可发布失败让管理员看到原因，
    也不要留下"显示已发布、检索不到"的假象。
    """
    document = _get_or_404(document_id)

    if document["status"] == KnowledgeStatus.ENABLED and not rebuild:
        # 启动同步路径的快速返回（管理员发布路径永远走重建）
        counts = _chunk_counts([document_id])
        if counts.get(document_id, 0) > 0:
            return _with_chunk_count(document, document_id)

    # ---- ① 切分 + Embedding + 写入向量库（独立事务，事务外做外部调用）----
    chunk_count = _build_chunks(document, rebuild=rebuild)

    # ---- ② 全部成功之后才置为已发布 ----
    with session_scope() as db:
        current = knowledge_repository.get_document_for_update(db, document_id)
        if current is None:
            raise KnowledgeNotFoundError(f"知识文档 {document_id} 不存在")
        if any(current.get(key) != document.get(key)
               for key in ("content", "title", "category", "source", "version", "status", "updated_at")):
            raise ValidationError("知识文档在发布期间发生变更，请重新发布")
        if chunk_count == 0 and not knowledge_repository.has_chunks(db, document_id):
            raise ValidationError("知识文档没有有效分块，无法发布")
        updated = knowledge_repository.update_document(
            db, document_id, status=KnowledgeStatus.ENABLED
        )
    if updated is None:
        raise KnowledgeNotFoundError(f"知识文档 {document_id} 不存在")

    logger.info(
        "知识已发布：id=%s title=%s 分块=%s（切分 → Embedding → 向量库完成）",
        document_id,
        document["title"],
        chunk_count,
    )
    result = _with_chunk_count(updated, document_id)
    result["published"] = True
    return result


def _build_chunks(document: dict, *, rebuild: bool) -> int:
    """切分 + 向量化 + 入库（分块写 MySQL，索引写向量库）。返回分块数（0 表示正文切不出内容）。"""
    with session_scope() as db:
        # ingest_document 只 flush 不 commit，事务由 session_scope 收口
        return ingest_document(db, document, force=rebuild)


def _purge_chunks(document_id: int) -> None:
    """把一篇文档的分块与向量从**两个存储**里清掉。

    只清 MySQL 的话，向量索引里会留下检索不到的孤儿点：既占空间，
    又让"索引点数 vs 库里分块数"的对账一直对不上（每次启动都触发一次重建）。
    """
    with session_scope() as db:
        knowledge_repository.clear_chunks(db, document_id)
    drop_document_index(document_id)


def _get_or_404(document_id: int) -> dict:
    """取文档，不存在就抛业务异常（404 KNOWLEDGE_NOT_FOUND）。

    统一在这里抛，是为了让"文档不存在"只有一种表现：
    Router 不用各自判断 None，前端也只处理一种错误码。
    """
    with session_scope() as db:
        document = knowledge_repository.get_document(db, document_id)
    if document is None:
        raise KnowledgeNotFoundError(f"知识文档 {document_id} 不存在")
    return document


def _with_chunk_count(document: dict, document_id: int) -> dict:
    """给单篇文档补上 chunk_count（列表/详情接口都用它判断"发布过没有"）。"""
    counts = _chunk_counts([document_id])
    result = dict(document)
    result["chunk_count"] = counts.get(document_id, 0)
    return result


def _chunk_counts(document_ids: list[int]) -> dict[int, int]:
    """批量取分块数（一次查询搞定，避免列表接口 N+1）。"""
    with session_scope() as db:
        return knowledge_repository.count_chunks_by_document(db, document_ids)


def _bump_version(version: str) -> str:
    """1.0 → 1.1（知识变更留痕，v2.md §34 的 version 字段）。

    版本号不合法（被人手工改成 "abc"）时退回 "1.1" 而不是抛错：
    留痕失败不该让"改知识"这个操作整个失败。
    """
    try:
        major, minor = version.split(".")
        return f"{major}.{int(minor) + 1}"
    except (ValueError, TypeError):
        return "1.1"


def _validate_status(status: str | None) -> None:
    """校验 PATCH 传进来的状态值，并引导管理员走 publish / offline。

    为什么不直接允许改 status：那两条路径除了改状态还要做别的事 ——
    发布要"切分 + 向量化"，下线要留分块。允许裸改 status 就会出现
    "状态是已发布、但向量库里没有"的假发布。所以这里只放行合法取值，
    并在报错文案里明确告诉调用方该用哪个接口。
    """
    if status is not None and status not in KnowledgeStatus.ALL:
        raise ValidationError(
            f"知识状态不合法：{status}（可选 {'/'.join(KnowledgeStatus.ALL)}；"
            "发布/下线请用 publish / offline 接口）"
        )


__all__ = [
    "KnowledgeStatus",
    "create_document",
    "delete_document",
    "get_document",
    "list_documents",
    "offline_document",
    "publish_all_drafts",
    "publish_document",
    "reindex",
    "search",
    "stats",
    "update_document",
]
