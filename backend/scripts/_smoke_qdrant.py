"""临时冒烟：Qdrant 索引（真数据）——启动自检、检索、维度不一致（验证完即删）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.embeddings import LocalHashEmbeddings
from app.rag.index import ensure_index_ready, index_status, rebuild_index
from app.rag.retriever import build_context, retrieve
from app.rag.vector_store import active_backend_name, get_vector_store, reset_vector_store

print("后端（配置/生效）:", active_backend_name())
store = get_vector_store()
print("store:", store.name, store.describe())

print("\n-- 启动前对账 --")
print(index_status())

print("\n-- 启动自检（应自动从 MySQL 灌入 57 个点）--")
print(ensure_index_ready())

print("\n-- 内容检索（用与库里一致的 512 维本地向量器提问）--")
vector = LocalHashEmbeddings().embed(["七天无理由退货规则是什么"])[0]
hits = store.search(vector, top_k=3)
for hit in hits:
    print("  ·", hit["source"], hit["score"], (hit["content"] or "")[:40].replace("\n", " "))

print("\n-- 维度不一致（1024 维查询）--")
print("144".__len__(), store.search([0.1] * 1024, top_k=3))

print("\n-- 分类过滤 --")
print(len(store.search(vector, top_k=3, category="refund_policy")), "条（refund_policy）")
print(len(store.search(vector, top_k=3, category="不存在的分类")), "条（不存在的分类）")

print("\n-- remove / rebuild --")
print("删掉文档 1 的点:", store.remove_document(1), "→ 剩余点数", store.count())
print("重建（recreate=True）:", rebuild_index(recreate=True))
print("重建后对账:", index_status())

reset_vector_store()
print("\n-- 关闭后重新打开（锁释放检查）--")
print("重新打开:", get_vector_store().describe())
print("\nretriever 走过的路径（会因远端 1024 维 vs 索引 512 维而告警并返回空）:")
print(" 命中:", len(retrieve("七天无理由退货")), "| context 长度:", len(build_context(retrieve("七天无理由退货"))))
reset_vector_store()
