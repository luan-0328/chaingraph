"""向量索引同步：把 MySQL 里的分块（真相源）灌进外部向量索引（Qdrant）。

为什么要单独一层
----------------
    MySQL      分块正文 + 向量 —— 真相源（发布、改稿、下线都以它为准）
    Qdrant     向量 + 检索元数据 —— 索引（派生物）

派生物就需要三件事，正好是本模块的全部内容：

    ① 跟着写     `index_chunks()`       发布/入库时把这篇文档的分块推进索引
    ② 跟着删     `drop_document_index()` 文档被删 / 正文改动作废旧向量时清掉它的点
    ③ 能对账     `index_status()` / `ensure_index_ready()` / `rebuild_index()`
                 索引丢了、被别的进程写歪了、换过 embedding 模型了，都能从 MySQL 重灌

③ 是"敢把向量库当纯索引"的前提：**任何不一致都能自愈，且不需要重算 Embedding**
（向量就在 MySQL 里，重灌只是搬运）。

一个刻意的取舍：索引写失败 = 发布失败
------------------------------------
`index_chunks()` 不吞异常：写索引失败时让发布整体失败（状态停在草稿）。
理由和 `knowledge_service.publish_document()` 里那句"先算向量、成功之后才置 ENABLED"一样 ——
吞掉异常就会留下"管理员以为发布了、Agent 永远检索不到"的假发布。
反过来，"删索引点"失败则只记日志：那一步的真相源变化（删文档/作废向量）已经生效，
残留的孤儿点本来就检索不到，不值得为它把删除操作也搞失败。
"""

from __future__ import annotations

from collections import Counter

from app.core.database import session_scope
from app.core.exceptions import VectorStoreUnavailableError
from app.core.logging import get_logger
from app.repositories import knowledge_repository
from app.rag.vector_store import get_vector_store, vector_store_info

logger = get_logger(__name__)


# ======================================================================
# 跟着写 / 跟着删
# ======================================================================
def index_chunks(document_id: int, chunks: list[dict]) -> int:
    """把一篇文档的分块写进外部向量索引，返回写入点数。

    在"分块刚写进 MySQL"之后调用（同一篇文档整体重建，见协议里的 upsert 约定）。
    MySQL 后端（回退模式）下是空操作：分块与向量本来就在 knowledge_chunks，没有第二份索引。

    **失败会抛 `VectorStoreUnavailableError`**（→ 503），让调用方的发布动作整体失败 ——
    留下"已发布但检索不到"的知识比"发布失败"糟得多。
    """
    store = get_vector_store()
    if not store.external_index:
        return 0

    try:
        return store.upsert(document_id, chunks)
    except Exception as exc:  # noqa: BLE001 —— 统一翻译成"向量库不可用"，前端/管理员要看懂
        logger.error("向量索引写入失败：document_id=%s error=%s", document_id, exc)
        raise VectorStoreUnavailableError(
            f"向量索引写入失败：{exc}（分块未落库，知识仍是草稿；"
            "请检查向量库后重新发布，或调 POST /api/admin/knowledge/reindex 重建索引）"
        ) from exc


def drop_document_index(document_id: int) -> int:
    """清掉某篇文档在向量索引里的全部点，返回删除点数。

    文档被删除、或正文改动导致旧向量作废时调用。
    **失败只记日志不抛**：真相源的改动（分块已清）已经生效，残留的点也检索不到
    （检索只查已发布文档的 id），没必要让"删除知识"这个操作跟着失败。
    """
    store = get_vector_store()
    if not store.external_index:
        return 0

    remove = getattr(store, "remove_document", None)
    if not callable(remove):  # pragma: no cover - 目前只有 Qdrant 实现有这个方法
        return 0

    try:
        return int(remove(document_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "清理向量索引失败（不影响业务，下次启动自检或 reindex 会清掉）：document_id=%s error=%s",
            document_id,
            exc,
        )
        return 0


# ======================================================================
# 对账 / 自愈
# ======================================================================
def index_status() -> dict:
    """索引对账：索引里有多少点，库里应该有多少点。

    字段说明：
        points       索引里实际有多少个点
        expected     库里带向量的分块数（**维度与索引一致的那些**，见 skipped_dim）
        in_sync      points == expected
        skipped_dim  维度对不上、因此灌不进去的分块数（换过 embedding 模型就会有）

    走的是两条轻查询（按维度 group by 的 COUNT + 最新一条的维度），
    不会把向量本身拉出来 —— 所以放心的在知识库概览这种接口里调。
    """
    with session_scope() as db:
        by_dim = knowledge_repository.count_chunks_by_dim(db)
        newest_dim = knowledge_repository.latest_embedding_dim(db)

    total = sum(by_dim.values())
    target, skipped = _target_dim(by_dim, newest_dim)

    info = vector_store_info()
    backend = info.get("backend")
    status: dict = {
        "backend": backend,
        "mode": info.get("mode"),
        "location": info.get("location"),
        "collection": info.get("collection"),
        "dim": info.get("dim"),
        "chunks_total": total,
        "expected": by_dim.get(target, 0) if target else 0,
        "skipped_dim": skipped,
        "points": info.get("points", 0),
    }
    if info.get("error"):
        status["error"] = info["error"]
        status["in_sync"] = False
        return status

    if backend != "qdrant":
        # MySQL 后端：分块与向量同库，天然一致（"索引"就是它自己）
        status["points"] = total
        status["expected"] = total
        status["in_sync"] = True
        status["note"] = "分块与向量同库（未启用到外部向量索引），无需同步"
        return status

    status["in_sync"] = bool(status["expected"]) and status["points"] == status["expected"]
    return status


def ensure_index_ready() -> dict:
    """启动自检：索引与库对不上就补建，**不重新调 Embedding**。

    专门覆盖"从 MySQL 版切到 Qdrant"这一步：库里已经有向量（上一次是用 numpy 检索的），
    但 Qdrant 里空空如也 —— 这里直接把分块与向量搬过去，几十毫秒的事，
    不需要管理员手动跑一遍 ingest（那会重新调一遍 Embedding，白花钱还可能改变向量）。

    方向不同，修法不同：
        索引少了点  → 增量灌入（不删集合，正在进行的检索不受影响）
        索引多了点  → 说明有孤儿（文档删了但点还在）→ 整体重建，保证是精确镜像
    自检失败**不阻塞启动**：向量库是加速件，降级时 RAG 少召回，业务接口照常。
    """
    try:
        store = get_vector_store()
        if not store.external_index:
            return index_status()

        status = index_status()
        if status.get("in_sync") or status.get("error"):
            if status.get("error"):
                logger.warning("向量索引自检失败（RAG 可能少召回）：%s", status["error"])
            return status

        recreate = int(status.get("points") or 0) > int(status.get("expected") or 0)
        logger.warning(
            "向量索引与库不一致（索引=%s 库=%s）→ 开始重建索引"
            "（从 MySQL 搬运向量，不重新调用 Embedding）",
            status.get("points"),
            status.get("expected"),
        )
        rebuild_index(recreate=recreate)
        return index_status()
    except Exception as exc:  # noqa: BLE001 —— 自检失败绝不能挡住启动
        logger.warning("向量索引自检失败（RAG 可能少召回）：%s", exc)
        return {"in_sync": False, "error": str(exc)}


def rebuild_index(*, recreate: bool = True) -> dict:
    """把 MySQL 里所有带向量的分块整体灌进向量索引。

    两条路径：
        recreate=True   先删掉集合再建（**保证是精确镜像**，孤儿点一并清掉）
        recreate=False  保留集合，按文档逐篇覆盖写（点 id 是确定性的，重写即幂等）

    重建期间检索会短暂返回空（集合被删掉的那一瞬间）——
    生产环境该用两个集合 + 别名切换做"蓝绿重建"，本项目规模下直接重建更简单，
    而且**真相源一直在 MySQL**，最坏情况也只是这几百毫秒里少召回几条。
    """
    store = get_vector_store()
    if not store.external_index:
        return {"backend": store.name, "indexed": 0, "skipped": "当前后端不需要外部索引"}

    with session_scope() as db:
        chunks = knowledge_repository.list_chunks(db, enabled_only=False)
        newest_dim = knowledge_repository.latest_embedding_dim(db)

    by_dim: Counter = Counter()
    for chunk in chunks:
        embedding = chunk.get("embedding")
        if embedding:
            by_dim[len(embedding)] += 1
    target, skipped = _target_dim(dict(by_dim), newest_dim)

    usable = [chunk for chunk in chunks if chunk.get("embedding") and len(chunk["embedding"]) == target]
    if not usable:
        logger.info("库里还没有带向量的分块，索引重建跳过")
        return {"backend": store.name, "indexed": 0, "expected": 0, "skipped_dim": {}}

    if skipped:
        logger.warning(
            "库里存在多种向量维度（换过 Embedding 模型但没重建）%s：索引按最新维度 %s 建，"
            "其余 %s 个分块需要 POST /api/admin/knowledge/ingest?force=true 重算后才会进索引",
            dict(by_dim),
            target,
            sum(skipped.values()),
        )

    if recreate:
        recreate_collection = getattr(store, "recreate_collection", None)
        if callable(recreate_collection):
            recreate_collection(target)

    indexed = 0
    grouped = _by_document(usable)
    for document_id, document_chunks in grouped.items():
        indexed += store.upsert(document_id, document_chunks)

    result = {
        "backend": store.name,
        "indexed": indexed,
        "documents": len(grouped),
        "dim": target,
        "recreated": recreate,
        "skipped_dim": skipped,
    }
    logger.info("向量索引重建完成：%s", result)
    return result


# ======================================================================
# 内部
# ======================================================================
def _target_dim(by_dim: dict[int, int], newest_dim: int | None) -> tuple[int | None, dict[int, int]]:
    """挑出"索引该用的维度"与"灌不进索引的那些维度"。

    规则：**以最新写入的那个分块的维度为准** —— 向量算完就写，
    所以最新一条必然出自当前 Embedding 模型，而索引要服务的正是**现在**的查询向量。
    （若按"历史多数派"建，换模型过渡期就会出现"新模型算出的查询一个都匹配不上"，
    那等于 RAG 直接失效；按最新维度建，至少新的知识立刻可用。）

    其余维度的分块如实报出来（知识库概览的 `skipped_dim` 与日志都会写），
    它们要等 `POST /api/admin/knowledge/ingest?force=true` 重算向量之后才会回到索引里。
    没有最新维度信息时退回多数派。
    """
    if not by_dim:
        return None, {}
    target = newest_dim if newest_dim in by_dim else max(by_dim, key=lambda dim: by_dim[dim])
    return target, {dim: count for dim, count in by_dim.items() if dim != target}


def _by_document(chunks: list[dict]) -> dict[int, list[dict]]:
    """按文档分组（upsert 的语义是"整篇重建"，所以必须一篇一篇写）。"""
    grouped: dict[int, list[dict]] = {}
    for chunk in chunks:
        grouped.setdefault(int(chunk["document_id"]), []).append(chunk)
    return grouped


__all__ = [
    "drop_document_index",
    "ensure_index_ready",
    "index_chunks",
    "index_status",
    "rebuild_index",
]
