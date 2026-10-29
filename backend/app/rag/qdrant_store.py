"""Qdrant 向量库：知识分块的向量索引与相似度检索（RAG 的检索后端）。

分工（v2.md §34「Vector DB 保存 embedding / chunk / metadata」）
--------------------------------------------------------------
    MySQL     文档本体（knowledge_documents）+ 分块正文与向量（knowledge_chunks）——**真相源**
    Qdrant    分块的向量 + 检索用的元数据（payload）——**索引**，随时可从 MySQL 重建

为什么 MySQL 里还要留一份向量（明明 Qdrant 里也存了）
    ① 重建索引不必重新调 Embedding：换后端 / 索引丢了 / 换了机器，
       直接从 MySQL 灌一遍即可（几十万块也就一次批量写，不花 Embedding 的钱）；
    ② 降级可用：Qdrant 连不上时 RAG 退回 MySQL 全量余弦，功能不中断（见 vector_store.py）。
    代价是向量存了两份 —— 换来的是"索引是纯派生物"，丢了不心疼。

两种运行模式（QDRANT_URL 决定）
    server  连 Qdrant 服务（docker run qdrant/qdrant，或 Qdrant Cloud），多实例可并发读写
    local   进程内嵌，数据落在 QDRANT_PATH —— 零部署，但目录**独占**：多实例会打不开

"能不能被检索到"的开关在哪（本文件最要紧的一条约定）
--------------------------------------------------
    search 的候选集**由 MySQL 的 knowledge_documents.status 决定**：
    先查出所有 ENABLED 文档的 id，再作为 `document_id IN (...)` 过滤条件交给 Qdrant。
    也就是说 Qdrant 里没有第二份"发布状态"副本，下线/改稿/删除**不需要通知它**，
    也就不存在"某条路径忘了同步状态 → 检索到已下线知识"这种漏洞
    （状态改动的路径不止一条：publish / offline / 直接 PATCH status）。
    代价：文档数很多时过滤条件会变长；上千篇以后应改成在 payload 里存 status，
    并在这里的 `set_status()` 式接口上同步 —— 那时"唯一开关"就从 MySQL 挪到了 Qdrant。
"""

from __future__ import annotations

import atexit
import gc
import shutil
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import get_logger
from app.repositories import knowledge_repository

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不需要装 qdrant-client
    from qdrant_client import QdrantClient

logger = get_logger(__name__)

#: 单次 upsert 的点数：一批太大容易顶到请求体上限（默认 32MB），太小则网络往返多
BATCH_SIZE = 256

#: 点 id 的命名空间：同一 (文档, 分块序号) 永远映射到同一个点 id，
#: 于是"重新发布"天然是**覆盖**而不是"又插一份"（不必先去重）
POINT_NAMESPACE = uuid.UUID("6f3f1a4e-1f2b-4c7d-9a0e-2b5c8d7e4f10")


class QdrantUnavailable(RuntimeError):
    """Qdrant 用不了（没装客户端 / 连不上服务 / 本地目录被别的进程占着）。

    这是**可降级**的异常：调用方（vector_store.get_vector_store）会退回 MySQL 实现，
    而不是让整个 RAG 挂掉 —— 向量库是加速件，不该成为单点。
    """


class QdrantVectorStore:
    """Qdrant 实现（满足 vector_store.VectorStore 协议）。

    客户端是**懒加载**的：构造它不产生任何 I/O，第一次真正用的时候才连
    （这样少了 Qdrant 也不影响应用启动、不影响别的接口）。
    """

    #: 后端名（日志 / 健康检查里显示）
    name = "qdrant"
    #: 分块与向量的真相源在 MySQL，本实现把它**镜像**成一份可检索的索引
    external_index = True

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
        path: str = "",
        collection: str = "",
        timeout: float | None = None,
    ) -> None:
        """记下连接参数（不连接）。url 为空 = 本地内嵌模式。"""
        self.url = (url or "").strip()
        self.api_key = (api_key or "").strip()
        self.path = str(path or "")
        self.collection = collection or settings.qdrant_collection
        self.timeout = timeout if timeout is not None else settings.qdrant_timeout
        self.mode = "server" if self.url else "local"

        self._client: QdrantClient | None = None
        #: 集合当前维度（None = 还没查过）。用它挡掉"换了 embedding 模型但没重建向量"的查询
        self._dim: int | None = None
        atexit.register(self.close)

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls) -> QdrantVectorStore:
        """按 .env 配置构造（server / local 两种模式由 QDRANT_URL 决定）。"""
        return cls(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            path=str(settings.qdrant_local_path),
            collection=settings.qdrant_collection,
            timeout=settings.qdrant_timeout,
        )

    def client(self) -> QdrantClient:
        """取客户端（首次调用时建立连接）。

        任何"用不了"的情况都翻译成 `QdrantUnavailable`，并带上**下一步该做什么**：
        没装包 → 提示 uv add；连不上 → 提示起服务或改配置；目录被占 → 提示换 server 模式。
        """
        if self._client is not None:
            return self._client

        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - 依赖已声明，装好就不会走到
            raise QdrantUnavailable(
                f"未安装 qdrant-client（{exc}）：在 backend/ 下执行 uv sync（或 uv add qdrant-client）"
            ) from exc

        try:
            if self.mode == "server":
                self._client = QdrantClient(
                    url=self.url,
                    api_key=self.api_key or None,
                    timeout=self.timeout,
                    # 只留 HTTP：本地单机服务用不上 gRPC，少一个端口少一类故障
                    prefer_grpc=False,
                )
            else:
                # 本地内嵌模式：目录独占（同一目录第二个进程会直接报错，被上层降级处理）
                self._client = QdrantClient(path=self.path)
        except Exception as exc:  # noqa: BLE001 —— 打不开就降级，不在这里区分具体错误类型
            raise QdrantUnavailable(self._explain(exc)) from exc

        return self._client

    def probe(self) -> None:
        """自检：能不能连上（连不上抛 QdrantUnavailable）。

        在选定后端时调用一次：让"配置错了"在启动日志里就说清楚，
        而不是等到第一次检索才发现（那时用户看到的是"没检索到知识"）。
        """
        try:
            self.client().get_collections()
        except QdrantUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QdrantUnavailable(self._explain(exc)) from exc

    def close(self) -> None:
        """关闭连接（本地内嵌模式下同时释放目录锁）。

        Windows 上如果拖到解释器退出时才由 `__del__` 关，
        portalocker 会在模块卸载后去 unlock 而报一串 "Exception ignored" ——
        所以这里显式关一次（构造时已注册到 atexit），重复调用是安全的。
        """
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 —— 关闭失败不该影响退出流程
            logger.debug("关闭 Qdrant 客户端出错：%s", exc)

    # ------------------------------------------------------------------
    # 集合维护
    # ------------------------------------------------------------------
    def ensure_collection(self, dim: int) -> bool:
        """确保集合存在且维度正确；维度变了就**重建集合**成新维度。

        返回是否可用（False = 这次写不进去，调用方自己决定要不要当错误）。

        为什么维度不一致时敢直接重建而不是报错：Qdrant 里的点全是 MySQL 的派生物，
        重建索引不需要重新调 Embedding。真正的数据（分块正文与向量）在 MySQL，
        一条都不会丢。反过来"保留旧维度的索引"只会让检索永远返回空。

        代价要说清楚（日志里也写了）：集合里**旧维度的点会一起被清掉**，
        它们本来也检索不到了（查询向量已经是新维度）。库里那些还没重算的旧向量，
        要等 `POST /api/admin/knowledge/ingest?force=true` 重算之后才会回到索引里。
        """
        if dim <= 0:
            logger.warning("向量维度未知（%s），跳过集合检查：应该先算一次 Embedding", dim)
            return False

        client = self.client()
        if not client.collection_exists(self.collection):
            self._create_collection(dim)
            return True

        current = self.collection_dim()
        if current != dim:
            logger.warning(
                "Qdrant 集合维度不一致（索引=%s 本次向量=%s）→ 按新维度重建集合："
                "通常意味着换了 Embedding 模型；库里其余未重算的分块请用 "
                "POST /api/admin/knowledge/ingest?force=true 重算后才会重新进索引",
                current,
                dim,
            )
            self.recreate_collection(dim)
        return True

    def collection_dim(self) -> int | None:
        """集合的向量维度（集合不存在则 None）。"""
        if self._dim is not None:
            return self._dim
        client = self.client()
        if not client.collection_exists(self.collection):
            return None
        config = client.get_collection(self.collection).config.params.vectors
        # 具名向量配置时 vectors 是 dict；本项目只用一个默认向量，兼容取第一个
        self._dim = int(config.size if hasattr(config, "size") else next(iter(config.values())).size)
        return self._dim

    def recreate_collection(self, dim: int) -> None:
        """删掉集合重建（维度按 dim）。

        用于"换 embedding 模型""索引对不上要精确重来"这两种情况：
        敢直接删是因为索引里没有任何"只此一份"的数据 —— 正文与向量都在 MySQL，
        重灌一遍就是了（不花 Embedding 的钱）。

        本地内嵌模式必须走 `_purge_local_storage()`（关客户端 → 删目录 → 再打开）：
        直接 `delete_collection` 在 Windows 上会因为自己的 sqlite 句柄**静默删不掉**，
        新集合随即把旧向量数组加载回来（维度还是老的），
        写新维度向量时报 `could not broadcast input array from shape (1024,) into shape (512,)`。
        """
        if self.mode == "local":
            self._purge_local_storage()

        client = self.client()
        if client.collection_exists(self.collection):
            client.delete_collection(self.collection)
        self._dim = None
        self._create_collection(dim)
        logger.info("Qdrant 集合已重建：%s（dim=%s，点数已清空，接下来从 MySQL 重灌）", self.collection, dim)

    def count(self) -> int:
        """索引里现在有多少个点（对账 / 概览用）。"""
        client = self.client()
        if not client.collection_exists(self.collection):
            return 0
        return int(client.count(collection_name=self.collection, exact=True).count)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def upsert(self, document_id: int, chunks: list[dict]) -> int:
        """重建某篇文档的向量索引，返回写入的点数。

        **整篇重建**语义（先删这篇文档的旧点，再写新的），与协议里说的一致：
        分块是按新正文重切的、序号会整体位移，增量写会让新正文的第 1 块
        和旧正文的第 5 块混在一起，检索命中哪一块全凭运气。

        chunks 为空表示"这篇文档现在没有向量了"（正文改回草稿、文档被删）——
        于是本方法退化成"清掉它的点"，调用方不用再学一个新方法。

        `wait=True` 不能省：Qdrant 默认异步落索引，省了它就会出现
        "刚发布却检索不到"（测试里那一步是立刻检索的）。
        """
        usable = [chunk for chunk in chunks if chunk.get("embedding")]
        if not usable:
            return self.remove_document(document_id)

        if not self.ensure_collection(len(usable[0]["embedding"])):
            return 0

        # 客户端要在 ensure_collection **之后**取：本地内嵌模式下"维度不一致"会关掉旧客户端
        # 再重开一个（见 recreate_collection），先取到的那个已经作废了
        client = self.client()

        # 先清旧点：不先删的话，文档从 10 块变成 5 块时后 5 块会永远留着
        self.remove_document(document_id)

        points = [self._to_point(document_id, index, chunk) for index, chunk in enumerate(usable)]
        for batch in _batched(points, BATCH_SIZE):
            client.upsert(collection_name=self.collection, points=batch, wait=True)

        logger.info(
            "向量索引写入：document_id=%s 分块=%s（%s）",
            document_id,
            len(points),
            self.collection,
        )
        return len(points)

    def remove_document(self, document_id: int) -> int:
        """删掉某篇文档的全部点，返回删除条数。

        文档被删除、或正文改动导致旧向量作废时调用。**不删也不会被检索到**
        （search 只查 ENABLED 文档的 id），但留着就是永远查不到的孤儿点，
        既占存储又让"点数对不上"的对账一直报警。
        """
        client = self.client()
        if not client.collection_exists(self.collection):
            return 0
        before = self.count()
        client.delete(
            collection_name=self.collection,
            points_selector=self._document_selector(document_id),
            wait=True,
        )
        return max(before - self.count(), 0)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = 4,
        category: str | None = None,
    ) -> list[dict]:
        """按向量召回 top_k 个分块，返回 [{title, source, category, content, score}]。

        两处过滤，都在这一次查询里：
            ① `document_id IN (已发布文档)` —— 未发布（草稿）/ 已下线的知识
               连参与打分的资格都没有，这就是"下线立刻检索不到"的落点；
            ② category —— 管理员按分类检索调试时才用。
        score 是余弦相似度（集合按 Cosine 建），越大越相关，与旧实现的数值口径一致。
        """
        if not query_vector:
            return []

        document_ids = self._retrievable_document_ids()
        if not document_ids:
            logger.info("知识库里没有已发布文档的分块，检索返回空")
            return []

        client = self.client()
        dim = self.collection_dim()
        if dim is None:
            logger.info("Qdrant 集合还不存在（知识还没入过库），检索返回空")
            return []
        if dim != len(query_vector):
            logger.warning(
                "向量维度不一致（索引=%s 查询=%s）：请重新入库知识库"
                "（POST /api/admin/knowledge/ingest?force=true）",
                dim,
                len(query_vector),
            )
            return []

        from qdrant_client import models

        conditions: list[Any] = [
            models.FieldCondition(key="document_id", match=models.MatchAny(any=document_ids))
        ]
        if category:
            conditions.append(
                models.FieldCondition(key="category", match=models.MatchValue(value=category))
            )

        try:
            response = client.query_points(
                collection_name=self.collection,
                query=list(query_vector),
                limit=top_k,
                query_filter=models.Filter(must=conditions),
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001 —— 分两种：集合没了 vs 后端真出问题
            if not client.collection_exists(self.collection):
                # 索引被人手工删了（或换了机器）：这是"没有知识"，不是故障。
                # 顺手丢掉缓存的维度，下一次写入/自检会按新维度重建集合。
                self._dim = None
                logger.warning("Qdrant 集合不存在（索引被清空过？）→ 本次检索返回空：%s", exc)
                return []
            raise

        hits: list[dict] = []
        for point in response.points:
            # 余弦可以是负的（完全不相关）；旧实现把 <=0 当"没查到"，这里保持口径一致
            if point.score <= 0:
                continue
            payload = point.payload or {}
            hits.append(
                {
                    "title": payload.get("title"),
                    "source": payload.get("source"),
                    "category": payload.get("category"),
                    "content": payload.get("content"),
                    "score": round(float(point.score), 4),
                }
            )
        return hits

    # ------------------------------------------------------------------
    # 自述
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        """后端自述（健康检查 / 知识库概览里显示"现在到底用的哪个向量库"）。"""
        info: dict = {
            "backend": self.name,
            "mode": self.mode,
            "location": self.url or self.path,
            "collection": self.collection,
        }
        try:
            if not self.client().collection_exists(self.collection):
                # 集合没了（换机器 / 被手工删过）：如实报 0，并丢掉缓存的维度，
                # 免得后续拿着一份不存在的索引的维度去比对
                self._dim = None
                info["points"] = 0
                info["dim"] = None
            else:
                info["points"] = self.count()
                info["dim"] = self.collection_dim()
        except Exception as exc:  # noqa: BLE001 —— 自述不该因为连不上就抛
            info["error"] = str(exc)
        return info

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _retrievable_document_ids(self) -> list[int]:
        """当前"能被检索到"的文档 id（= MySQL 里 status=ENABLED 的那些）。

        这是本实现里唯一的状态判据：Qdrant 里不存第二份发布状态，
        所以下线 / 改稿 / 删除都**不需要通知向量库**，也就不会出现
        "某条改状态的路径忘了同步 → 检索到不该给用户看的规则"。
        """
        with session_scope() as db:
            return knowledge_repository.list_retrievable_document_ids(db)

    def _to_point(self, document_id: int, index: int, chunk: dict) -> Any:
        """把一条分块转成 Qdrant 点（向量 + payload）。

        payload 里存**检索结果要用的元数据**（正文、标题、来源、分类），
        这样命中之后不用再回 MySQL 取一次正文；
        但**不存 embedding** —— 它已经是点本身，存进 payload 等于同一份数据写两遍。
        """
        from qdrant_client import models

        return models.PointStruct(
            id=self._point_id(document_id, chunk.get("chunk_index", index)),
            vector=list(chunk["embedding"]),
            payload={
                "document_id": document_id,
                "chunk_index": chunk.get("chunk_index", index),
                "content": chunk.get("content"),
                "title": chunk.get("title"),
                "source": chunk.get("source"),
                "category": chunk.get("category"),
                "embedding_model": chunk.get("embedding_model"),
            },
        )

    def _point_id(self, document_id: int, chunk_index: int) -> str:
        """点 id = (文档 id, 分块序号) 的确定性 UUID5。

        用确定性 id 而不是随机 id：同一篇文档重新发布时是**覆盖同一个点**，
        不必依赖"先删后写"也能保证不重复（删只是为了让缩短的文档不留尾巴）。
        """
        return str(uuid.uuid5(POINT_NAMESPACE, f"knowledge:{document_id}:{chunk_index}"))

    def _document_selector(self, document_id: int) -> Any:
        """"这篇文档的全部点"的选择器。"""
        from qdrant_client import models

        return models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
            )
        )

    def _create_collection(self, dim: int) -> None:
        """按维度建集合（并记下维度缓存、建 payload 索引）。"""
        self.client().create_collection(
            collection_name=self.collection,
            vectors_config=self._vector_params(dim),
        )
        self._dim = dim
        logger.info(
            "已创建 Qdrant 集合：%s（dim=%s, distance=Cosine, %s=%s）",
            self.collection,
            dim,
            self.mode,
            self.url or self.path,
        )
        self._create_payload_indexes()

    def _purge_local_storage(self) -> None:
        """本地内嵌模式：**把本地索引目录整个删掉**（关客户端 → 删目录 → 下次访问时重开）。

        为什么不是直接 `delete_collection`：本地模式内部用
        `shutil.rmtree(collection_dir, ignore_errors=True)` 删集合目录，
        而句柄就握在**我们自己**这个刚打开的客户端手里 —— Windows 上删不掉却不报错，
        于是"按新维度新建"的集合又把旧向量数组加载了回来（维度还是老的），
        写新维度向量时报 `could not broadcast input array from shape (1024,) into shape (512,)`。
        只有先把客户端关掉、再删目录，才能真正删干净。

        删除范围是 `QDRANT_PATH` 这一整个目录 —— 它本来就是**本项目的**本地索引目录
        （.env.example 里写明了），换维度时整体重建正是我们要的语义。
        服务端模式不受影响：那边只删这一个集合。
        """
        self.close()
        for _ in range(2):
            shutil.rmtree(self.path, ignore_errors=True)
            if not Path(self.path).exists():
                return
            # 句柄可能还挂在引用环上（QdrantLocal ↔ LocalCollection）：回收后再试一次
            gc.collect()

        raise QdrantUnavailable(
            f"本地索引目录删不掉：{self.path}（文件仍被占用）。"
            "请确认没有别的实例在用同一个目录，或改用服务端模式（QDRANT_URL）"
        )

    def _vector_params(self, dim: int) -> Any:
        """向量参数：Cosine 距离（与旧实现的余弦口径一致）+ 显式 HNSW 参数。

        HNSW 用 Qdrant 默认档（m=16、ef_construct=100）——
        几万块规模下召回与延迟都够，改小会掉召回，改大会变慢，没什么可调的。
        """
        from qdrant_client import models

        return models.VectorParams(
            size=dim,
            distance=models.Distance.COSINE,
            hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
        )

    def _create_payload_indexes(self) -> None:
        """给过滤字段建 payload 索引（`document_id` / `category`）。

        本地内嵌模式没有索引这个概念（客户端会提示"payload indexes have no effect"），
        所以只在服务端模式建，免得日志里一堆无意义的告警。
        """
        if self.mode != "server":
            return
        from qdrant_client import models

        for field, schema in (
            ("document_id", models.PayloadSchemaType.INTEGER),
            ("category", models.PayloadSchemaType.KEYWORD),
            ("content", models.PayloadSchemaType.TEXT),
        ):
            try:
                self.client().create_payload_index(
                    collection_name=self.collection, field_name=field, field_schema=schema
                )
            except Exception as exc:  # noqa: BLE001 —— 索引只是加速，建不上不该挡发布
                logger.warning("创建 payload 索引失败：field=%s error=%s", field, exc)

    def _explain(self, exc: Exception) -> str:
        """把底层异常翻译成"人话 + 下一步动作"（这段文案会进启动日志）。"""
        if self.mode == "local":
            if "already accessed" in str(exc):
                return (
                    f"本地 Qdrant 目录被另一个进程占用（{self.path}）："
                    "本地模式同一目录只能被一个进程打开；"
                    "多实例请起服务端并把 QDRANT_URL 指向它"
                    "（docker run -p 6333:6333 qdrant/qdrant）"
                )
            return f"打不开本地 Qdrant 目录 {self.path}：{exc}"
        return f"连不上 Qdrant 服务 {self.url}：{exc}（起服务或把 VECTOR_STORE 改成 mysql）"


def _batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    """把点分批（一次请求写太多会顶到请求体上限）。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


__all__ = ["BATCH_SIZE", "QdrantUnavailable", "QdrantVectorStore"]
