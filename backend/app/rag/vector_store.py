"""向量库：存知识分块与向量，并提供相似度检索（RAG 的检索后端选择层）。

两个实现，按 `.env` 的 `VECTOR_STORE` 选择，**调用方（ingest / retriever）完全不感知**：

    QdrantVectorStore（默认，见 qdrant_store.py）
        MySQL 存分块与向量的**真相源**，Qdrant 存一份可检索的**索引**（HNSW + payload 过滤）。
        检索走 ANN，不再全量反序列化；索引随时能从 MySQL 重建（不用重算 Embedding）。

    MySQLVectorStore（回退，本文件）
        向量以 JSON 存在 knowledge_chunks.embedding，检索时全量取出用 numpy 算余弦。
        知识量在几千块以内完全够用，且零外部依赖 ——
        Qdrant 没起 / 连不上 / 本地目录被别的进程占着时自动退到这里，RAG 不会因此瘫掉。

为什么要有回退（而不是"没有 Qdrant 就报错"）
    向量库是**加速件**，不是业务真相：检索不到知识，Agent 还能靠 Tools 与规则回答；
    但整个 RAG 挂掉会连带让验收里的"知识库发布/下线对 RAG 的影响"整条链路失效。
    所以选后端时就做好降级，并把降级**大声说出来**（WARNING 日志 + /health + 知识库概览），
    绝不让"其实在用 MySQL"这件事悄悄发生。

想再换一个后端（pgvector / Milvus / 云端向量库）？
    实现下面 VectorStore 协议里的两个方法（search / upsert），再改 `get_vector_store()`
    的分支即可 —— 这正是把接口抽出来的原因。
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import get_logger
from app.repositories import knowledge_repository
from app.rag.qdrant_store import QdrantUnavailable, QdrantVectorStore

logger = get_logger(__name__)


class VectorStore(Protocol):
    """向量库接口（换实现的唯一契约）。

    只有两个方法，因为它们就是"知识入库"与"知识检索"的全部动作。
    换 Qdrant / pgvector：实现这个协议，然后改 `get_vector_store()` 的返回值 ——
    ingest / retriever 完全不用动。

    实现方必须遵守两条隐含约定（它们决定了"下线知识检索不到"能否成立）：
        ① `search` 只能返回**已发布文档**的分块。当前两个实现都是"查库拿
           `status = ENABLED` 的文档 id，再作为过滤条件下推到向量库"——
           状态只有一个真相（MySQL），向量库里不存第二份副本，所以
           下线 / 改稿 / 删除都不需要通知向量库，也就不存在"漏同步"的路径。
        ② `upsert` 是**整篇重建**语义（先删该文档的旧分块再写新的）——
           增量更新会让"改过的正文"和"旧分块"混在同一个文档里。
    """

    #: 后端名（日志 / 健康检查用）
    name: str
    #: 分块与向量的真相源是否在别处、本实现只是索引（决定要不要做索引同步）
    external_index: bool

    def search(self, query_vector: list[float], *, top_k: int, category: str | None = None) -> list[dict]:
        """按向量召回 top_k 个分块，返回 [{title, source, category, content, score}]。

        实现方应当**过滤掉未发布文档**并保证 score 越大越相关。
        """
        ...

    def upsert(self, document_id: int, chunks: list[dict]) -> int:
        """重建某文档的全部分块，返回写入条数。

        **整篇替换**语义（先删旧分块再写新的），不是增量 upsert。
        原因：分块是按新正文重新切出来的，索引会整体位移 ——
        增量写会让同一篇文档里混着"新正文的第 1 块"和"旧正文的第 5 块"，
        检索命中哪一块全凭运气。
        """
        ...


class MySQLVectorStore:
    """MySQL + numpy 的向量检索（回退实现，也是索引重建的数据来源）。"""

    #: 后端名（日志 / 健康检查里显示）
    name = "mysql"
    #: 分块与向量本来就写在 knowledge_chunks，不需要额外的索引同步
    external_index = False

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 4,
        category: str | None = None,
    ) -> list[dict]:
        """全量取分块 → 算余弦 → 取前 top_k。

        **"下线知识检索不到"就发生在 `enabled_only=True` 这一句**：
        它让 SQL 直接把未发布 / 已下线文档的分块排除在候选集之外，
        所以它们连打分的机会都没有。这也是唯一一处过滤 ——
        Agent 侧不存在第二个开关，不存在"某条路径忘了过滤"。

        为什么是全量取而不是向量库索引：知识量在几千块以内时这样最简单、零运维。
        上到十万级就该用 Qdrant（默认已是它），本实现退居"Qdrant 不可用时的兜底"。

        维度不一致（换过 embedding 模型、旧向量还没重算）时**只比维度对得上的那些**，
        其余记一条 warning —— 既不能拿两个模型的向量算相似度（分数没有意义），
        也不能因为库里混着旧维度就直接抛异常。
        """
        with session_scope() as db:
            chunks = knowledge_repository.list_chunks(db, enabled_only=True)

        if category:
            chunks = [chunk for chunk in chunks if chunk.get("category") == category]

        # 维度必须先过滤，再拼矩阵：库里可能同时存在新旧模型的向量（换过 embedding 还没重建），
        # 直接 np.asarray 会得到"参差不齐"的数组而**抛异常**（500），
        # 而不是"检索不到"。只比较维度对得上的，其余如实告警。
        query = np.asarray(query_vector, dtype=np.float32)
        candidates = [
            chunk
            for chunk in chunks
            if chunk.get("embedding") and len(chunk["embedding"]) == query.shape[0]
        ]
        if not candidates:
            logger.info("向量库为空（还没入库知识），检索返回空")
            return []

        skipped = len([chunk for chunk in chunks if chunk.get("embedding")]) - len(candidates)
        if skipped:
            dims = sorted({len(chunk["embedding"]) for chunk in chunks if chunk.get("embedding")})
            logger.warning(
                "有 %s 个分块的向量维度与查询（%s）不一致，已跳过：库里维度=%s。"
                "换过 Embedding 模型的话，请重新入库知识库"
                "（POST /api/admin/knowledge/ingest?force=true）",
                skipped,
                query.shape[0],
                dims,
            )

        matrix = np.asarray([chunk["embedding"] for chunk in candidates], dtype=np.float32)

        # 向量都已 L2 归一化 → 余弦相似度 = 点积
        scores = matrix @ query
        order = np.argsort(-scores)[:top_k]

        hits: list[dict] = []
        for index in order:
            chunk = candidates[int(index)]
            score = float(scores[int(index)])
            if score <= 0:
                continue
            hits.append(
                {
                    "title": chunk.get("title"),
                    "source": chunk.get("source"),
                    "category": chunk.get("category"),
                    "content": chunk.get("content"),
                    "score": round(score, 4),
                }
            )
        return hits

    def upsert(self, document_id: int, chunks: list[dict]) -> int:
        """重建某个文档的分块（调用方负责先把 embedding 算好）。

        注意它**不参与检索路径上的写入**：分块本来就在 knowledge_chunks，
        入库时由 ingest 直接写；本方法只为满足协议而存在（换后端时语义对齐）。
        """
        with session_scope() as db:
            return knowledge_repository.replace_chunks(db, document_id, chunks)

    def describe(self) -> dict:
        """后端自述（健康检查 / 知识库概览里显示"现在到底用的哪个向量库"）。"""
        try:
            with session_scope() as db:
                points = knowledge_repository.count_indexed_chunks(db)
        except Exception as exc:  # noqa: BLE001 —— 自述不该因为查库失败就抛
            return {"backend": self.name, "mode": "inline", "error": str(exc)}
        return {
            "backend": self.name,
            "mode": "inline",
            "location": f"{settings.database_name}.knowledge_chunks",
            "points": points,
        }


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """取当前生效的向量库实现（进程内单例）。

    **换实现只改这一个函数**：调用方（ingest / retriever）一行都不用动 ——
    这正是把 `VectorStore` 协议抽出来的目的。

    选择顺序：
        ① `VECTOR_STORE=qdrant`（默认）且 Qdrant 能连上 → QdrantVectorStore
        ② 否则（没起 Qdrant / 目录被别的进程占着 / 配置写错）→ MySQLVectorStore
    降级时打 WARNING，并把原因写成"下一步该做什么"；`/health` 与知识库概览
    也能看到实际生效的后端，不会让人误以为在用 Qdrant。

    单例是安全的：两个实现都不持有数据库会话（每次 search/upsert 自己开 session_scope），
    所以不存在跨请求共享会话的问题。选了哪个后端在进程生命周期内固定 ——
    Qdrant 中途恢复要重启（或调用 reset_vector_store()）才会切回去。
    """
    global _store
    if _store is not None:
        return _store

    if settings.vector_store.strip().lower() == "qdrant":
        try:
            store = QdrantVectorStore.from_settings()
            store.probe()
        except QdrantUnavailable as exc:
            logger.warning("Qdrant 不可用，RAG 自动回退到 MySQL 向量检索：%s", exc)
        else:
            logger.info(
                "向量库就绪：qdrant（mode=%s location=%s collection=%s）",
                store.mode,
                store.url or store.path,
                store.collection,
            )
            _store = store
            return _store
    elif settings.vector_store.strip().lower() != "mysql":
        logger.warning(
            "VECTOR_STORE=%s 不是已知后端（可选 qdrant / mysql），按 mysql 处理",
            settings.vector_store,
        )

    _store = MySQLVectorStore()
    return _store


def reset_vector_store() -> None:
    """丢掉当前单例（下次 get 时按配置重新选，并释放 Qdrant 本地目录锁）。

    用途：单测要换一个临时目录的 Qdrant；运维脚本要就地换后端。
    应用正常运行时不需要调它 —— 后端在进程内固定，切后端重启即可。
    """
    global _store
    close = getattr(_store, "close", None)
    if callable(close):
        close()
    _store = None


def active_backend_name() -> str:
    """当前生效的后端名（**不触发初始化**）。

    没建过单例就报配置值：健康检查是要被高频调用的，不能让它顺手去连一次 Qdrant。
    """
    return _store.name if _store is not None else settings.vector_store.strip().lower() or "mysql"


def vector_store_info() -> dict:
    """向量库现状（后端 / 位置 / 集合 / 点数），给健康检查与知识库概览用。

    会真的去问一次后端（本地模式还要开目录），所以只在"看一眼现状"的接口里调，
    不要放进 /health 这种高频路径。
    """
    store = get_vector_store()
    describe = getattr(store, "describe", None)
    if callable(describe):
        return describe()
    return {"backend": store.name}


__all__ = [
    "MySQLVectorStore",
    "QdrantVectorStore",
    "VectorStore",
    "active_backend_name",
    "get_vector_store",
    "reset_vector_store",
    "vector_store_info",
]
