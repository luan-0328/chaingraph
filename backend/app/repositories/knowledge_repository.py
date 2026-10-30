"""知识库数据访问（文档 + 分块 + 向量）。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeStatus


# ======================================================================
# 文档
# ======================================================================
def create_document(
    db: Session,
    *,
    title: str,
    content: str,
    source: str | None = None,
    category: str | None = None,
    version: str = "1.0",
    status: str = KnowledgeStatus.ENABLED,
) -> dict:
    """新建知识文档（只写文档本体，**不切分、不算向量**）。

    仓储默认 status=ENABLED，但**调用方应当显式传状态**：
        · 管理员接口建文档 → 传 DRAFT（草稿，发布后才进向量库）
        · `knowledge/*.md` 目录同步 → 用默认 ENABLED（产品自带知识本来就该生效）
    默认值选 ENABLED 是为了让目录同步那条路径不啰嗦；
    真正的门禁在 Service 层（services/knowledge_service.create_document）。

    切分与向量化由 app/rag/ingest.py 负责，不在仓储里做 ——
    仓储只管 SQL，不调外部 Embedding 服务。
    """
    document = KnowledgeDocument(
        title=title, content=content, source=source, category=category, version=version, status=status
    )
    db.add(document)
    db.flush()
    db.refresh(document)
    return document.to_dict()


def get_document(db: Session, document_id: int) -> dict | None:
    """按主键取知识文档（字典形式，含正文）。"""
    document = db.get(KnowledgeDocument, document_id)
    return document.to_dict() if document else None


def get_document_by_source(db: Session, source: str) -> dict | None:
    """按来源文件名查文档 —— `knowledge/*.md` 同步时用它判断"这篇入库没有"。

    判据用 source（文件名）而不是 title：标题可以被管理员改，
    文件名是目录同步的稳定标识，用它才能做到"重复启动不重复插入"。
    """
    document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source == source))
    return document.to_dict() if document else None


def get_document_for_update(db: Session, document_id: int) -> dict | None:
    """锁定并刷新知识文档，供发布时核对向量化期间的版本变化。"""
    document = db.scalar(select(KnowledgeDocument).where(
        KnowledgeDocument.id == document_id).with_for_update().execution_options(populate_existing=True))
    return document.to_dict() if document else None


def list_documents(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    keyword: str | None = None,
    status: str | None = None,
) -> tuple[list[dict], int]:
    """知识文档列表（可按分类/标题关键词/状态筛），返回 (当页数据, 总数)。

    注意 `keyword` 只匹配**标题**，不搜正文：正文可能很长，
    对正文做 LIKE '%...%' 既慢又容易命中一堆无关文档；
    "按内容找知识"应该走 `/admin/knowledge/search`（向量检索），那才是它的用途。

    `status` 是新加的筛选项：管理员最常问的就是"哪些还没发布"，
    所以列表接口要能直接筛 DRAFT。
    """
    conditions = []
    if category:
        conditions.append(KnowledgeDocument.category == category)
    if keyword:
        conditions.append(KnowledgeDocument.title.like(f"%{keyword}%"))
    if status:
        conditions.append(KnowledgeDocument.status == status)

    count_stmt = select(func.count()).select_from(KnowledgeDocument)
    list_stmt = select(KnowledgeDocument)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(list_stmt.order_by(KnowledgeDocument.id.desc()).limit(limit).offset(offset))
    return [document.to_dict() for document in rows], total


def update_document(
    db: Session,
    document_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
    status: str | None = None,
    version: str | None = None,
) -> dict | None:
    """部分更新知识文档（None = 不改该字段）。

    **本方法只改字段，不改分块与向量** —— 这是刻意的分工：
    "正文变了要作废旧向量、要退回草稿"属于业务规则，在 Service 层做
    （见 services/knowledge_service.update_document）。
    如果让仓储顺手清向量，那"改个标题"也会把向量删掉，代价太大且难以察觉。
    """
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        return None

    if title is not None:
        document.title = title
    if content is not None:
        document.content = content
    if category is not None:
        document.category = category
    if status is not None:
        document.status = status
    if version is not None:
        document.version = version

    db.flush()
    db.refresh(document)
    return document.to_dict()


def delete_document(db: Session, document_id: int) -> bool:
    """删除文档本体，返回是否删到了行。

    **调用方必须先 `clear_chunks`**（见 services/knowledge_service.delete_document）：
    分块表没有外键级联，只删文档会留下永远检索不到的孤儿向量，
    白白占用存储、还会让向量检索反复反序列化它们。
    """
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        return False
    db.delete(document)
    db.flush()
    return True


def count_documents_by_status(db: Session) -> dict[str, int]:
    """各状态各有几篇（知识库概览用）。

    返回的 dict **保证包含全部状态键**（没有的补 0）——
    否则前端拿不到 `DRAFT: 0` 会显示成空白，而不是"没有草稿"。
    """
    rows = db.execute(
        select(KnowledgeDocument.status, func.count()).group_by(KnowledgeDocument.status)
    ).all()
    counts = {status: int(total) for status, total in rows}
    for status in KnowledgeStatus.ALL:
        counts.setdefault(status, 0)
    return counts


# ======================================================================
# 分块 + 向量
# ======================================================================
def replace_chunks(db: Session, document_id: int, chunks: list[dict]) -> int:
    """重建某文档的全部分块（重新入库知识时调用）。"""
    db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).delete()
    db.flush()

    rows = [
        KnowledgeChunk(
            document_id=document_id,
            chunk_index=chunk["chunk_index"],
            content=chunk["content"],
            embedding=chunk.get("embedding"),
            embedding_model=chunk.get("embedding_model"),
            title=chunk.get("title"),
            source=chunk.get("source"),
            category=chunk.get("category"),
        )
        for chunk in chunks
    ]
    db.add_all(rows)
    db.flush()
    return len(rows)


def list_chunks(db: Session, *, enabled_only: bool = True) -> list[dict]:
    """取出全部分块（含向量）用于检索 / 索引重建。

    `enabled_only=True` 时只给"已发布"文档的分块（MySQL 版检索实现走这条路）；
    索引重建要的是**全部**分块，包括已下线文档的（向量保留是为了重新发布时直接复用）。
    """
    stmt = select(KnowledgeChunk).join(
        KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id
    )
    if enabled_only:
        stmt = stmt.where(KnowledgeDocument.status == KnowledgeStatus.ENABLED)

    rows = db.scalars(stmt.order_by(KnowledgeChunk.id.asc()))
    return [chunk.to_dict(with_embedding=True) for chunk in rows]


def list_retrievable_document_ids(db: Session) -> list[int]:
    """所有"能被检索到"的知识文档 id（status=ENABLED）。

    它是外部向量库（Qdrant）检索时的候选集：把"能不能被检索到"这件事
    始终留给 MySQL 这一处状态来判断 —— 向量库里不再存第二份发布状态，
    于是下线 / 改稿 / 删除都不需要"记得同步向量库"，也就没有漏同步的路径。
    """
    rows = db.scalars(
        select(KnowledgeDocument.id).where(KnowledgeDocument.status == KnowledgeStatus.ENABLED)
    )
    return [int(row) for row in rows]


def count_chunks(db: Session) -> int:
    """库里一共有多少分块（含已下线文档的、也算上草稿残留）。

    它回答的是"占了多少存储"，而不是"能被检索到多少"——
    后者是 `list_chunks(enabled_only=True)` 的长度。
    两个数不一样是正常的（下线的知识保留分块以便复用向量）。
    """
    return int(db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0)


def count_indexed_chunks(db: Session) -> int:
    """真正带向量的分块数（发布过才算；草稿文档没有向量）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.embedding.is_not(None))
        )
        or 0
    )


def count_chunks_by_dim(db: Session) -> dict[int, int]:
    """按**向量维度**统计分块数（对账用：索引是什么维度、库里有多少块对得上）。

    用 MySQL 的 `JSON_LENGTH` 在库侧算长度，而不是把向量拉回 Python 再 len()：
    后者在对账这种"只要一个数"的场景里，等于把几十 MB 的向量反序列化一遍。

    为什么需要它：换过 Embedding 模型后，库里会同时存在旧维度与新维度的分块，
    而一个向量索引只能有一个维度 —— 对账时要能一眼看出"多数派是哪个维度、
    有多少块灌不进去"（`{维度: 块数}`）。
    """
    rows = db.execute(
        select(func.json_length(KnowledgeChunk.embedding), func.count())
        .where(KnowledgeChunk.embedding.is_not(None))
        .group_by(func.json_length(KnowledgeChunk.embedding))
    ).all()
    return {int(dim): int(total) for dim, total in rows if dim is not None}


def latest_embedding_dim(db: Session) -> int | None:
    """**最新写入**的那个分块的向量维度（没有向量则 None）。

    它代表"当前 Embedding 模型算出来的维度"：向量总是算完就写，
    所以最新一条的维度就是当前模型的维度。换模型过渡期（库里新旧维度混杂）时，
    向量索引该按它来建 —— 索引要服务的是**现在**的查询向量，
    而不是库里的历史多数派（按多数派建会让新模型算出来的查询一个都匹配不上）。
    """
    return db.scalar(
        select(func.json_length(KnowledgeChunk.embedding))
        .where(KnowledgeChunk.embedding.is_not(None))
        .order_by(KnowledgeChunk.id.desc())
        .limit(1)
    )


def count_chunks_by_document(db: Session, document_ids: list[int]) -> dict[int, int]:
    """批量统计每篇文档有多少分块（前端一列搞定"发布过没有"）。

    为什么按 document_ids 批量查而不是循环单查：
    列表页 20 篇文档会被循环 20 次 —— 列表接口最容易踩的 N+1。
    """
    if not document_ids:
        return {}
    rows = db.execute(
        select(KnowledgeChunk.document_id, func.count())
        .where(KnowledgeChunk.document_id.in_(document_ids))
        .group_by(KnowledgeChunk.document_id)
    ).all()
    return {int(document_id): int(total) for document_id, total in rows}


def has_chunks(db: Session, document_id: int) -> bool:
    """这篇文档切过分块没有（"跳过重复入库"的判据）。

    以前这里是把**全部分块连同向量**拉出来再在 Python 里 any(...)：
    知识量一上来（几千块 × 512 维 float）就是几十 MB 的无谓反序列化，
    而答案只是一个布尔值。改成 COUNT 之后代价与知识量无关。
    """
    return bool(
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document_id)
        )
    )


def clear_chunks(db: Session, document_id: int) -> int:
    """删掉某文档的全部分块与向量，返回删除条数。

    "正文改了"必须调用它：旧分块留着，检索就会命中**过时知识** ——
    拿已经改掉的规则去回答用户，比检索不到更危险。
    """
    deleted = db.query(KnowledgeChunk).filter(
        KnowledgeChunk.document_id == document_id
    ).delete()
    db.flush()
    return int(deleted or 0)
