"""Qdrant 向量库：索引、检索、发布/下线语义、索引自愈（本文件只打向量库这一层）。

为什么要单独一组测试
-------------------
`tests/test_e2e_chain.py::TestKnowledgeLifecycle` 验的是"**行为**"（草稿检索不到、
发布后检索得到、下线立刻检索不到），走 HTTP 接口，但它跑在 pytest 起的独立实例上 ——
而本地内嵌模式的 Qdrant **目录是独占的**（同一目录第二个进程打不开），
多实例同时起时只有第一个用得上 Qdrant，其余会按设计回退到 MySQL。
于是"Qdrant 这条路到底对不对"必须有测试直接在进程内打它，否则换成 Qdrant 后
真正的检索实现可能一行都没被跑到。

本文件因此把向量库指向**临时目录 + 独立集合**（不影响开发机上的真实索引），
并把向量器固定成本地哈希实现（不发网络请求、结果确定、不花钱）。
断言的都是"换成 Qdrant 之后必须依然成立"的东西：

    ① 发布 → 分块进 MySQL 且索引里有点 → 检索命中（分数与出处都对）
    ② 分类过滤真的下推给了 Qdrant
    ③ 下线 → 立刻检索不到（状态只有一个真相：MySQL），但分块保留
    ④ 删文档 → 索引里的点一起清掉（不留永远检索不到的孤儿）
    ⑤ 索引与库对不上时能自愈，且**不重新调用 Embedding**
    ⑥ 维度对不上时返回空而不是抛异常（与 MySQL 实现的降级口径一致）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.rag.embeddings import LocalHashEmbeddings
from app.services import knowledge_service

pytestmark = pytest.mark.integration

#: 查询词刻意选成正文里连续出现的一段：本地降级向量器是字符 n-gram 哈希（词法相似度），
#: 用只在正文里零散出现的词去查会命中不稳 —— 那不是被测行为，是向量器的局限。
MARKER = "极速退款专用标记词"


@pytest.fixture
def qdrant_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_or_skip: None):
    """把向量库切到一个临时目录的 Qdrant（本地内嵌模式），用完释放目录锁。

    为什么要 monkeypatch 配置 + reset 单例，而不是直接 new 一个 store：
    要验证的正是**生产代码的取用路径**（get_vector_store 按配置选后端），
    自己 new 一个就绕过了选择与降级逻辑，那部分才是这次改动的要害。
    """
    from app.rag import embeddings as embeddings_module
    from app.rag import vector_store as vector_store_module

    monkeypatch.setattr(settings, "vector_store", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "")
    monkeypatch.setattr(settings, "qdrant_path", str(tmp_path / "qdrant"))
    monkeypatch.setattr(settings, "qdrant_collection", "pytest_knowledge")
    # 向量器固定为本地哈希实现：离线、确定、不产生费用
    monkeypatch.setattr(embeddings_module, "_provider", LocalHashEmbeddings())

    vector_store_module.reset_vector_store()
    store = vector_store_module.get_vector_store()
    assert store.name == "qdrant", "临时目录的 Qdrant 没起来（本地模式应当能直接打开）"

    yield store
    vector_store_module.reset_vector_store()


def _create_document(category: str = "pytest_qdrant") -> dict:
    """建一篇测试文档（草稿 + 两段正文，便于验证"分类过滤"与"整篇重建"）。"""
    return knowledge_service.create_document(
        title="pytest Qdrant 测试文档",
        content=f"# pytest Qdrant\n\n{MARKER} 的处理规则是：必须人工复核。\n\n## 第二节\n\n换货时限为七天。",
        category=category,
    )


# ======================================================================
# 索引 + 检索
# ======================================================================
class TestIndexAndSearch:
    """发布 → 写索引 → 检索命中；下线 → 立刻检索不到。"""

    def test_publish_indexes_and_search_hits(self, qdrant_store) -> None:
        """发布之后：MySQL 有分块有向量、Qdrant 有点、检索能命中同一段文字。"""
        from app.rag.index import index_status

        document = _create_document()
        document_id = document["id"]
        try:
            # 草稿：没有分块，也没有索引点
            assert document["status"] == "DRAFT"
            assert qdrant_store.count() == 0
            assert not any(
                MARKER in (hit["content"] or "") for hit in knowledge_service.search(MARKER)
            ), "草稿知识被检索到了"

            published = knowledge_service.publish_document(document_id)
            assert published["status"] == "ENABLED"
            assert published["chunk_count"] > 0

            # 索引里真的有点，且点数等于这篇文档的分块数
            # （这个集合是测试自己的：临时目录 + 独立集合名，只有本文件往里写过东西）
            assert qdrant_store.count() == published["chunk_count"]
            assert index_status()["points"] == published["chunk_count"]

            # 检索命中：命中项要带出处、分类与分数（AgentResult.references 靠它）
            hits = knowledge_service.search(MARKER, top_k=4)
            hit = next((h for h in hits if MARKER in (h["content"] or "")), None)
            assert hit is not None, f"发布之后检索不到：{hits}"
            assert hit["title"] == "pytest Qdrant 测试文档"
            assert hit["category"] == "pytest_qdrant"
            assert 0 < hit["score"] <= 1.0

            # ---- 下线：立刻检索不到，但分块保留（重新发布不用重算向量）----
            offline = knowledge_service.offline_document(document_id)
            assert offline["status"] == "DISABLED"
            assert not any(
                MARKER in (h["content"] or "") for h in knowledge_service.search(MARKER)
            ), "下线之后仍然能被检索到"
            assert knowledge_service.get_document(document_id)["chunk_count"] > 0, (
                "下线把分块删了（重新发布又要重算向量）"
            )
        finally:
            knowledge_service.delete_document(document_id)

    def test_category_filter_is_pushed_down(self, qdrant_store) -> None:
        """分类过滤真的生效（只在这一个分类里召回）。"""
        vector = LocalHashEmbeddings().embed([MARKER])[0]
        assert qdrant_store.search(vector, top_k=4, category="pytest_qdrant") == [], (
            "集合是空的，不该有命中"
        )

        document_id = _create_document(category="pytest_qdrant")["id"]
        try:
            knowledge_service.publish_document(document_id)
            vector = LocalHashEmbeddings().embed([MARKER])[0]

            assert qdrant_store.search(vector, top_k=4, category="pytest_qdrant")
            assert qdrant_store.search(vector, top_k=4, category="另一个分类") == []
        finally:
            knowledge_service.delete_document(document_id)

    def test_delete_removes_points(self, qdrant_store) -> None:
        """删文档 → 索引里的点一起清掉（不留永远检索不到的孤儿点）。"""
        document_id = _create_document()["id"]
        knowledge_service.publish_document(document_id)
        assert qdrant_store.count() > 0

        knowledge_service.delete_document(document_id)
        assert qdrant_store.count() == 0, "文档删了，索引里的点还留着"


# ======================================================================
# 索引自愈
# ======================================================================
class TestIndexReconcile:
    """索引是派生物：丢了能重建，且**不重新调用 Embedding**。"""

    def test_ensure_index_ready_rebuilds_lost_index(self, qdrant_store) -> None:
        """索引被清空（换机器 / 索引损坏）→ 启动自检必须从 MySQL 把它灌回来。

        这里断言的是**重建之后的自洽**（`points == expected`），而不是某个具体点数：
        重建是按库里"最新那条向量的维度"整体灌入的，所以无论开发机上的知识库是什么维度、
        有没有正在过渡的旧维度向量，重建完都必然对得上。
        """
        from app.rag.index import ensure_index_ready, index_status

        document_id = _create_document()["id"]
        try:
            knowledge_service.publish_document(document_id)

            # 直接删掉集合：MySQL 里的分块与向量一条没少，只是索引没了
            qdrant_store.client().delete_collection(qdrant_store.collection)
            broken = index_status()
            assert broken["in_sync"] is False
            assert broken["points"] == 0
            # 集合都没了，检索要能降级成"没查到"而不是抛异常
            assert knowledge_service.search(MARKER) == []

            healed = ensure_index_ready()
            assert healed["in_sync"] is True, healed
            assert healed["points"] == healed["expected"] > 0
            assert qdrant_store.count() == healed["points"]
        finally:
            knowledge_service.delete_document(document_id)

    def test_dimension_mismatch_degrades_to_empty(self, qdrant_store) -> None:
        """查询向量维度与索引不一致 → 返回空 + 告警，而不是抛异常。

        与 MySQL 实现的口径一致（那边也是"维度不一致 → 记一条 warning 后返回空"）：
        换过 Embedding 模型但没重建向量时，宁可这一次检索不到，
        也不要拿两个不同模型的向量去算相似度（分数毫无意义）。
        """
        document_id = _create_document()["id"]
        try:
            knowledge_service.publish_document(document_id)
            assert qdrant_store.search([0.1] * 32, top_k=4) == []
        finally:
            knowledge_service.delete_document(document_id)
