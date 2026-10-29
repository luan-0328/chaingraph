"""知识库（v2.md §34）。

分工：
    MySQL  存原始业务知识（knowledge_documents）+ 切分后的分块与向量（knowledge_chunks）
    Vector 层由 app/rag/vector_store.py 抽象：默认 Qdrant（app/rag/qdrant_store.py）
           存这次切分出来的向量与检索元数据，随时可从 MySQL 重灌（见 app/rag/index.py）；
           Qdrant 不可用时自动回退到本表里的向量 + numpy 全量余弦。

为什么向量也存 MySQL（既然用的是 Qdrant）？
    ① 重建索引不必重新调 Embedding（换后端、索引丢了、换机器，搬一趟即可）
    ② 向量库连不上时 RAG 能降级继续跑
    即"索引是纯派生物，丢了不心疼"，代价是向量存两份。

文档生命周期（状态就是"能不能被 Agent 检索到"的唯一开关）
----------------------------------------------------------
    DRAFT       草稿：只有文档本体，**不在向量库里**，Agent 检索不到
                  ↓ POST /admin/knowledge/{id}/publish（切分 → Embedding → 写向量）
    ENABLED     已发布：分块 + 向量已入库，Agent 能通过 RAG 检索到
                  ↓ POST /admin/knowledge/{id}/offline
    DISABLED    已下线：分块仍留在库里（→ 重新发布不用重新算向量），
                 但检索侧一律按 knowledge_documents.status 过滤 ——
                 两个检索实现（Qdrant 的过滤器 / MySQL 的 JOIN）都取这份状态，
                 下线的知识绝不会被回答引用

为什么"改内容"必须回到 DRAFT
    改了正文却不重算向量，检索出来的还是旧知识的语义 —— 那比检索不到更糟
    （会拿过时的规则回答用户）。所以更新 content 时状态自动退回 DRAFT，
    逼管理员显式发布一次，把"切分 + 向量化"这一步做实。
"""

from sqlalchemy import BigInteger, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin


class KnowledgeStatus:
    #: 草稿：未发布，向量库里没有它，Agent 检索不到
    DRAFT = "DRAFT"
    #: 已发布：分块与向量已入库，可被 RAG 检索
    ENABLED = "ENABLED"
    #: 已下线：分块保留但不参与检索
    DISABLED = "DISABLED"

    #: 能被 Agent 检索到的状态（唯一真相：向量库过滤条件也用它）
    RETRIEVABLE = (ENABLED,)
    ALL = (DRAFT, ENABLED, DISABLED)


class KnowledgeDocument(Base, TimestampMixin):
    """一篇知识文档（通常对应 knowledge/*.md 的一个文件）。"""

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=KnowledgeStatus.ENABLED
    )

    __table_args__ = (Index("idx_knowledge_documents_category", "category"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "category": self.category,
            "version": self.version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def __repr__(self) -> str:
        return f"<KnowledgeDocument id={self.id} title={self.title!r}>"


class KnowledgeChunk(Base):
    """切分后的知识分块 + 向量（RAG 检索的最小单元）。"""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 向量（JSON 数组）：这里是**真相源**，Qdrant 里的那份是它的索引副本。
    #: 之所以留一份：重建索引不用重新调 Embedding，且 Qdrant 挂了还能全量余弦兜底。
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 检索结果里要带回去的元数据（v2.md §23：references）
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("idx_knowledge_chunks_document_id", "document_id"),)

    def to_dict(self, *, with_embedding: bool = False) -> dict:
        data = {
            "id": self.id,
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "title": self.title,
            "source": self.source,
            "category": self.category,
        }
        if with_embedding:
            data["embedding"] = self.embedding
        return data

    def __repr__(self) -> str:
        return f"<KnowledgeChunk doc={self.document_id} idx={self.chunk_index}>"
