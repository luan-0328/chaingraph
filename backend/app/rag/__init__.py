"""rag：知识检索（RAG）层。

    加载/切分    splitter.py     Markdown → 知识块
    向量化       embeddings.py   文本 → 向量（远程 embedding 或本地降级）
    向量库       vector_store.py 后端选择：Qdrant（默认）/ MySQL 回退
                 qdrant_store.py Qdrant 实现（集合、写入、ANN 检索、payload 过滤）
    索引同步     index.py        索引对账与重建（从 MySQL 搬向量，不重算 Embedding）
    检索         retriever.py    问题 → Top K 知识片段 + 引用（references）
    入库         ingest.py       目录/文档 → 切分 → 向量 → 落 MySQL + 写向量索引

两个存储的分工（v2.md §34）：MySQL 存分块正文与向量（真相源），向量库存检索索引（派生物）。
铁律（v2.md §81 原则四）：
    RAG **只提供知识**（告诉 Agent 规则是什么），
    业务是否真的允许执行，由 Service 决定。
"""
